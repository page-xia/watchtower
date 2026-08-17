"""短期/长期趋势线最新值的持久缓存（活跃股榜单「低吸机会」过滤用）。

口径与详情页日K公式一致（趋势公式.md）：
- 短期 = 知行短期趋势线 EMA(EMA(C,10),10)
- 长期 = 知行多空线 (MA14+MA28+MA57+MA114)/4

日线K线走 easy_tdx（与详情页同一 fetch_daily_kline_rows），每只票一次网络
请求，因此结果按 ``computed_day`` 持久化到本地 JSON，当天内复用；缺失/过期
的代码交给后台小线程池补算，请求路径不阻塞。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from app.daily_formula_engine import _ma
from app.formula_engine import ema

logger = logging.getLogger(__name__)

# 低吸区间判定：短期/长期线取低者为底（向下 1% 缓冲）、高者为顶（向上 3% 缓冲），
# 现价落在 [底线×(1-1%), 顶线×(1+3%)] 即命中
NEAR_LINE_TOP_TOLERANCE = 0.03
NEAR_LINE_BOTTOM_TOLERANCE = 0.01
# 拉取日线根数：长期线需要 MA114，留足余量
KLINE_COUNT = 180
# 后台补算并发：tdx 请求为网络 IO，小线程池即可
WORKER_COUNT = 4
# 磁盘写入节流（秒）
_SAVE_DEBOUNCE_SECONDS = 5.0

RowsFetcher = Callable[[str, int], list[dict[str, Any]]]


def compute_trend_lines(closes: list[float]) -> dict[str, float | None]:
    """与 daily_formula_engine 主图公式同口径的最新短期/长期线值。"""
    if not closes:
        return {"zx_trend": None, "zx_duokong": None}
    zx_trend = ema(ema(closes, 10), 10)
    ma14 = _ma(closes, 14)
    ma28 = _ma(closes, 28)
    ma57 = _ma(closes, 57)
    ma114 = _ma(closes, 114)
    duokong_parts = (ma14[-1], ma28[-1], ma57[-1], ma114[-1])
    zx_duokong = sum(duokong_parts) / 4.0 if None not in duokong_parts else None
    return {
        "zx_trend": round(zx_trend[-1], 3) if zx_trend else None,
        "zx_duokong": round(zx_duokong, 3) if zx_duokong is not None else None,
    }


def near_trend_match(
    price: float,
    entry: dict[str, Any],
    top_tolerance: float = NEAR_LINE_TOP_TOLERANCE,
    bottom_tolerance: float = NEAR_LINE_BOTTOM_TOLERANCE,
) -> bool:
    """低吸区间：短期/长期线低者为底（-1%）、高者为顶（+3%），现价落在区间内即命中。"""
    if not price or price <= 0:
        return False
    lines = [
        line
        for line in (entry.get("zx_trend"), entry.get("zx_duokong"))
        if line is not None and line > 0
    ]
    if not lines:
        return False
    low_line = min(lines)
    high_line = max(lines)
    return low_line * (1 - bottom_tolerance) <= price <= high_line * (1 + top_tolerance)


class TrendLineStore:
    """短期/长期线最新值缓存：内存 + 本地 JSON，后台线程池补算缺失代码。"""

    def __init__(self, file_path: Path, rows_fetcher: RowsFetcher) -> None:
        self._file = Path(file_path)
        self._fetch_rows = rows_fetcher
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._pending: set[str] = set()
        self._queue: list[tuple[str, str]] = []
        self._workers: list[threading.Thread] = []
        self._last_save_at = 0.0
        self._dirty = False
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self._file.exists():
                raw = json.loads(self._file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._entries = {
                        str(code).zfill(6): entry
                        for code, entry in raw.items()
                        if isinstance(entry, dict)
                    }
        except Exception as exc:  # pragma: no cover - 磁盘缓存损坏不致命
            logger.warning("trend line cache load failed: %s", exc)
            self._entries = {}

    def _save(self, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            if not self._dirty:
                return
            if not force and now - self._last_save_at < _SAVE_DEBOUNCE_SECONDS:
                return
            self._dirty = False
            self._last_save_at = now
            snapshot = dict(self._entries)
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._file)
        except Exception as exc:  # pragma: no cover
            logger.warning("trend line cache save failed: %s", exc)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def get_fresh(self, codes: Iterable[str], today: str) -> dict[str, dict[str, Any]]:
        """返回当天已计算的代码 → 线值；缺失/过期的代码不在结果里。"""
        result: dict[str, dict[str, Any]] = {}
        with self._lock:
            for code in codes:
                normalized = str(code or "").zfill(6)
                entry = self._entries.get(normalized)
                if entry and entry.get("computed_day") == today:
                    result[normalized] = entry
        return result

    # ------------------------------------------------------------------
    # 后台补算
    # ------------------------------------------------------------------
    def ensure(self, codes: Iterable[str], today: str) -> None:
        """把缺失/过期的代码排入后台补算队列（去重、有界），立即返回。"""
        queued = 0
        with self._lock:
            for code in codes:
                normalized = str(code or "").zfill(6)
                if len(normalized) != 6 or not normalized.isdigit():
                    continue
                entry = self._entries.get(normalized)
                if entry and entry.get("computed_day") == today:
                    continue
                if normalized in self._pending:
                    continue
                if len(self._queue) >= 6000:
                    break
                self._pending.add(normalized)
                self._queue.append((normalized, today))
                queued += 1
            if queued:
                self._ensure_workers_locked()

    def _ensure_workers_locked(self) -> None:
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        while self._queue and len(self._workers) < WORKER_COUNT:
            worker = threading.Thread(
                target=self._worker_loop,
                name="trend-line-warm",
                daemon=True,
            )
            self._workers.append(worker)
            worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    return
                code, day = self._queue.pop(0)
            try:
                rows = self._fetch_rows(code, KLINE_COUNT)
                closes = [float(row.get("close") or 0) for row in rows if float(row.get("close") or 0) > 0]
                values = compute_trend_lines(closes)
                bar_date = str(rows[-1].get("date") or "") if rows else ""
            except Exception as exc:
                logger.debug("trend line compute failed for %s: %s", code, exc)
                values = {"zx_trend": None, "zx_duokong": None}
                bar_date = ""
            with self._lock:
                self._pending.discard(code)
                # 算不出来的也记录当天结果，避免每个刷新周期重复打队列；
                # near_trend_match 对 None 线值天然不命中。
                self._entries[code] = {
                    **values,
                    "bar_date": bar_date,
                    "computed_day": day,
                }
                self._dirty = True
            self._save()

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def flush(self) -> None:
        self._save(force=True)

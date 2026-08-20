"""东财 push2 盘中全市场资金流（免费公开接口）。

边界（与 AGENTS.md 数据边界一致）：

- 只取**个股级资金数值**（主力/超大/大/中/小单净额与占比）；板块归组仍走
  easy_tdx 官方板块映射，东财板块 taxonomy 不进入本模块。
- 该口径是「按单笔成交额分桶」的推断值：实测（2026-08-17）
  主力净额 f62 = 超大单 f66 + 大单 f72，中+小单为其零和镜像。
  它和 Tushare moneyflow 同属推断口径，**不是隐藏主力单真值**，
  UI 一律标注「东财推断口径」。
- 非官方接口：可能变动/限频。策略 = TTL 缓存 + 请求路径零阻塞
  （缓存过期时在一次性守护线程里拉取，请求方先吃旧快照）+ 失败保留旧数据降级。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

import requests

from app.data_sources import china_now, is_trading_window

logger = logging.getLogger(__name__)

# 主域名 push2 在部分网络（本地过滤/WAF JA3）下会重置 Python TLS 连接，
# push2delay 镜像实测可直连；逐页失败时按序换域名重试。
API_URLS = (
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
)
# fs: 沪深 A 股（深证 m:0 t:6/80，上证 m:1 t:2/23），与全市场口径一致
FS_A_SHARE = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
FIELDS = "f12,f14,f2,f3,f62,f66,f72,f78,f84,f184"
PAGE_SIZE = 100  # push2delay 镜像每页封顶 100 条（实测 2026-08-18）
PAGE_TIMEOUT = 8.0
PAGE_SLEEP = 0.12
MAX_PAGES = 80  # 全市场 5549 只 / 100 = 56 页，80 页封顶保险丝

# 交易时段快照 TTL；非交易时段东财返回的是当日终值，6h 内无需重拉
TRADING_TTL_SECONDS = max(30, int(os.getenv("WATCH_EM_MONEYFLOW_TTL_SECONDS", "90")))
OFFHOURS_TTL_SECONDS = 6 * 3600

_EM_ENABLED = os.getenv("WATCH_EM_MONEYFLOW", "1").lower() in {"1", "true", "yes"}


def _parse_row(item: dict[str, Any]) -> dict[str, Any] | None:
    code = str(item.get("f12") or "").strip()
    if not code:
        return None

    def num(key: str) -> float | None:
        value = item.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    main_net = num("f62")
    if main_net is None:
        return None
    return {
        "code": code,
        "name": str(item.get("f14") or code),
        "price": num("f2") or 0.0,
        "change_pct": num("f3") or 0.0,
        # 东财金额字段单位为元，直接使用
        "main_net": main_net,
        "elg_net": num("f66") or 0.0,  # 超大单净额
        "lg_net": num("f72") or 0.0,  # 大单净额
        "md_net": num("f78") or 0.0,  # 中单净额
        "sm_net": num("f84") or 0.0,  # 小单净额
        "main_pct": num("f184") or 0.0,  # 主力净占比 %
    }


def _fetch_page(page: int) -> dict[str, Any]:
    """拉单页；按 API_URLS 顺序换域名重试，全部失败抛最后一个异常。"""
    last_exc: Exception | None = None
    for url in API_URLS:
        try:
            resp = requests.get(
                url,
                params={
                    "pn": page,
                    "pz": PAGE_SIZE,
                    "po": 1,
                    "np": 1,
                    "fltt": 2,
                    "invt": 2,
                    "fid": "f62",
                    "fs": FS_A_SHARE,
                    "fields": FIELDS,
                },
                timeout=PAGE_TIMEOUT,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) KimiWatch/1.0"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001 - 换下一个域名
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def fetch_full_market() -> dict[str, dict[str, Any]]:
    """拉全市场资金流（分页）。任一页失败抛异常，调用方负责降级。"""
    rows: dict[str, dict[str, Any]] = {}
    page = 1
    total: int | None = None
    while True:
        payload = _fetch_page(page)
        data = payload.get("data") or {}
        if total is None:
            total = int(data.get("total") or 0)
        diff = data.get("diff") or []
        for item in diff:
            row = _parse_row(item)
            if row:
                rows[row["code"]] = row
        if not diff or (total and page * PAGE_SIZE >= total):
            break
        page += 1
        if page > MAX_PAGES:
            break
        time.sleep(PAGE_SLEEP)
    return rows


class EMMoneyflowCache:
    """东财资金流快照缓存：请求路径只读；过期时后台一次性线程补齐。"""

    def __init__(
        self,
        enabled: bool | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        self._enabled = _EM_ENABLED if enabled is None else enabled
        self._on_update = on_update
        self._lock = threading.Lock()
        self._rows: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0  # time.monotonic 戳
        self._as_of: str = ""
        self._error: str = ""
        self._fetching = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _ttl(self) -> float:
        return TRADING_TTL_SECONDS if is_trading_window() else OFFHOURS_TTL_SECONDS

    def snapshot(self) -> dict[str, Any]:
        """只读缓存；过期则触发后台刷新但立即返回旧快照（可为空）。"""
        if not self._enabled:
            return {"available": False, "note": "已用 WATCH_EM_MONEYFLOW=0 关闭"}
        with self._lock:
            stale = (time.monotonic() - self._fetched_at) >= self._ttl() if self._fetched_at else True
            need_fetch = stale and not self._fetching
            if need_fetch:
                self._fetching = True
            rows = self._rows
            as_of = self._as_of
            error = self._error
        if need_fetch:
            thread = threading.Thread(target=self._fetch_once, name="em-moneyflow-fetch", daemon=True)
            thread.start()
        if not rows:
            return {
                "available": False,
                "note": error and f"东财资金流拉取失败：{error}（下轮自动重试）" or "东财资金流首屏拉取中",
            }
        return {
            "available": True,
            "as_of": as_of,
            "stock_count": len(rows),
            "rows": rows,
            "stale_error": error,  # 有旧数据但最近一次刷新失败时透出
        }

    def _fetch_once(self) -> None:
        try:
            rows = fetch_full_market()
            with self._lock:
                self._rows = rows
                self._fetched_at = time.monotonic()
                self._as_of = china_now().strftime("%H:%M:%S")
                self._error = ""
                self._fetching = False
            logger.info("em moneyflow snapshot refreshed: %d rows", len(rows))
        except Exception as exc:  # noqa: BLE001 - 失败保留旧快照降级
            logger.warning("em moneyflow fetch failed: %s", exc)
            with self._lock:
                # 无旧数据时退避 30s 再允许重试；有旧数据按正常 TTL 走
                if not self._rows:
                    self._fetched_at = time.monotonic() - self._ttl() + 30
                self._error = str(exc)[:120]
                self._fetching = False
        finally:
            if self._on_update is not None:
                try:
                    self._on_update()
                except Exception:  # noqa: BLE001 - 通知失败不得影响缓存刷新
                    logger.exception("em moneyflow update callback failed")

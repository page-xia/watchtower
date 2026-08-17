"""F10 聚合数据的持久缓存层。

设计说明：
- F10（财务三表/股东/分红/研报等）属于低频变化数据，盘中不需要实时拉取。
  本模块把 ``TushareF10DataSource.fetch`` 的 payload 持久化，让详情页打开
  走「内存 → 持久缓存 → 实时拉取」三层，重启/重部署后依然秒开。
- 生产环境（CloudBase）落 cloudbase nosql（每只股票一个 doc，超 64KB 自动
  zlib 压缩，由 ``CloudBaseNoSqlStateStore`` 处理）；本地开发落
  ``data/f10_cache/{code}.json``。
- 另维护一份 ``{code: fetched_ts}`` 索引，供每日盘前定时增量预热枚举候选
  股票（只刷缓存过期的，不重复全量拉）。
- 缓存读写失败一律静默兜底（log + 返回 None），绝不影响主流程。
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_INDEX_KEY = "_index"
_MAX_INDEX_ENTRIES = 512


def _normalize_key(key: str) -> str:
    """纯数字短码按股票代码 zfill(6)；带前缀的复合 key（如 capital_flow:300476）
    原样保留，仅在落本地文件时替换非法字符。"""
    text = str(key or "").strip()
    if text.isdigit() and len(text) < 6:
        return text.zfill(6)
    return text


def _safe_filename(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", key)


class F10CacheStore:
    """按股票代码持久化 F10 payload，带 fetched_ts 新鲜度索引。"""

    def __init__(
        self,
        cache_dir: Path | str,
        state_store: Any | None = None,
        *,
        namespace: str = "f10",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self._state_store = state_store
        self.namespace = namespace

    # -- 后端选择 -------------------------------------------------------------

    @property
    def _use_cloud(self) -> bool:
        store = self._state_store
        return store is not None and bool(getattr(store, "available", True))

    def _local_path(self, code: str) -> Path:
        return self.cache_dir / f"{_safe_filename(code)}.json"

    # -- 单只股票读写 ----------------------------------------------------------

    def load(self, code: str) -> dict[str, Any] | None:
        """返回 ``{"fetched_ts": float, "payload": {...}}``，无缓存或损坏返回 None。"""
        normalized = _normalize_key(code)
        try:
            doc = self._read_doc(normalized)
        except Exception as exc:
            logger.warning("f10 cache load failed: %s error=%r", normalized, exc)
            return None
        if not isinstance(doc, dict):
            return None
        payload = doc.get("payload")
        fetched_ts = doc.get("fetched_ts")
        if not isinstance(payload, dict) or not isinstance(fetched_ts, (int, float)):
            return None
        return doc

    def save(self, code: str, payload: dict[str, Any], fetched_ts: float | None = None) -> None:
        normalized = _normalize_key(code)
        ts_value = float(fetched_ts if fetched_ts is not None else time.time())
        doc = {"code": normalized, "fetched_ts": ts_value, "payload": payload}
        try:
            self._write_doc(normalized, doc)
            if normalized.isdigit() and len(normalized) == 6:
                self._touch_index(normalized, ts_value)
        except Exception as exc:
            logger.warning("f10 cache save failed: %s error=%r", normalized, exc)

    def age_seconds(self, code: str, now_ts: float | None = None) -> float | None:
        doc = self.load(code)
        if doc is None:
            return None
        return max(0.0, float(now_ts if now_ts is not None else time.time()) - float(doc["fetched_ts"]))

    # -- 索引（盘前预热枚举用） -------------------------------------------------

    def list_index(self) -> dict[str, float]:
        """``{code: fetched_ts}``，读取失败返回空表。"""
        try:
            if self._use_cloud:
                raw = self._state_store.get_json(self.namespace, _INDEX_KEY, default={})
            else:
                path = self._local_path(_INDEX_KEY)
                raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:
            logger.warning("f10 cache index load failed: %r", exc)
            return {}
        if not isinstance(raw, dict):
            return {}
        index: dict[str, float] = {}
        for key, value in raw.items():
            code = str(key).strip()
            if len(code) == 6 and code.isdigit() and isinstance(value, (int, float)):
                index[code] = float(value)
        return index

    def _touch_index(self, code: str, fetched_ts: float) -> None:
        index = self.list_index()
        index[code] = fetched_ts
        if len(index) > _MAX_INDEX_ENTRIES:
            ordered = sorted(index.items(), key=lambda item: item[1], reverse=True)
            index = dict(ordered[:_MAX_INDEX_ENTRIES])
        if self._use_cloud:
            self._state_store.set_json(self.namespace, _INDEX_KEY, index)
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._local_path(_INDEX_KEY)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

    # -- 底层读写 -------------------------------------------------------------

    def _read_doc(self, code: str) -> dict[str, Any] | None:
        if self._use_cloud:
            return self._state_store.get_json(self.namespace, code, default=None)
        path = self._local_path(code)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_doc(self, code: str, doc: dict[str, Any]) -> None:
        if self._use_cloud:
            self._state_store.set_json(self.namespace, code, doc)
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._local_path(code)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, default=str), encoding="utf-8")
        tmp.replace(path)

"""暗盘资金盯盘模块（2026-08-18 重构）。

「暗盘资金」拆成四个可回答的口径，数据源分层严格标注（见 AGENTS.md 数据边界）：

1. 背景条：全市场主力净额（Tushare moneyflow EOD 合计）＋ 东财盘中主力净额合计
   ＋ 北向成交额（moneyflow_hsgt；2024-08 起交易所只披露成交额，无净额/方向）
   ＋ 融资余额日变化（margin_detail，T+1 晚间落地）＋ 大宗交易当日总额。
2. 暗吸 / 暗派（核心）：Tushare moneyflow 多日窗口 × daily_basic 收盘价/换手。
   暗吸 = 近 5 日主力净额为正、净流入天数 ≥ 3 且区间涨跌幅 |≤3%|
   （资金在进、价格没动）；暗派对称。「暗」来自多日连续 + 价格背离，
   单日大额是「明」不是「暗」。
3. 大手场外：北向十大成交（hsgt_top10，仅成交额）＋ 大宗交易（折溢价）
   ＋ 龙虎榜机构席位净买（top_inst 中「机构」席位聚合）。
4. 盘中资金地图：东财 push2 全市场资金流（推断口径，见 em_moneyflow.py 注释），
   板块归组一律走 easy_tdx 申万 1/2/3 级映射，不用第三方 taxonomy。

个股摘要 stock_payload(code)：详情页右侧「暗盘资金」区使用。

存储（2026-08-18 起 sqlite → 统一 EOD 访问层，见 app/eod_store.py）：

- 本地：pymysql 直连 MySQL（watchtower_eod 库 eod_* 表），
  由 scripts/ingest_eod_tushare.py 收盘后落库；
- 生产云托管：容器访问不到本地库，EOD 面板/个股摘要由收盘管线预计算成
  快照推送 CloudBase NoSQL（ingest --push-prod），本模块读快照并用容器
  自己的 easy_tdx 映射回填名称/板块。

性能边界（首页不能被拖慢）：

- HTTP 端点只读内存缓存 / EOD 存储（带 TTL），永远不在请求路径上发
  行情网络请求；东财快照由 em_moneyflow.EMMoneyflowCache 后台一次性线程补齐；
- 原「24 只自选股磁带大单推断慢循环」已下线（2026-08-18）：样本太小、
  口径与详情页磁带重复；逐笔磁带回归「个股详情按需读取」的既定边界。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from app.config import AppSettings
from app.data_sources import china_now, market_session
from app.eod_store import EodStore, build_eod_store

logger = logging.getLogger(__name__)

# 暗吸/暗派判定窗口与阈值
ABSORB_WINDOW_DAYS = 5       # 近 N 个交易日
ABSORB_MIN_DAYS = 3          # 其中同向净流入天数 ≥ 3
ABSORB_MAX_PRICE_PCT = 3.0   # 价格「没动」：区间涨跌幅 |≤3%|
ABSORB_MIN_NET = 30_000_000  # 窗口净额低于 3000 万不参与（噪声）

EOD_CACHE_TTL_SECONDS = 300


class DarkPoolMonitor:
    """暗盘资金监控：官方口径 EOD 存储读取 + 东财盘中快照 + 板块联动过滤。"""

    def __init__(
        self,
        settings: AppSettings,
        context_provider: Callable[[], Any],
        sector_mapper: Callable[[int], dict[str, str]] | None = None,
        sector_members_provider: Callable[[int], dict[str, list[str]]] | None = None,
        em_cache: Any = None,
        eod_store: EodStore | None = None,
    ) -> None:
        self.settings = settings
        self._context_provider = context_provider
        self._sector_mapper = sector_mapper
        self._sector_members_provider = sector_members_provider
        self._em = em_cache
        self._store = eod_store or build_eod_store(settings)

        self._eod_cache: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # 对外入口：只读缓存，绝不阻塞
    # ------------------------------------------------------------------
    def payload(self, sector: str | None = None, board_level: int = 3) -> dict[str, Any]:
        eod = self._eod_payload()
        em_section = self._em_section()

        sector_filter = self._resolve_sector_filter(sector, board_level)
        member_codes: set[str] | None = None
        if sector_filter and sector_filter["member_count"] > 0:
            member_codes = set(sector_filter.pop("member_codes"))

        def pick(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            if member_codes is None:
                return rows
            return [r for r in rows if str(r.get("code") or "") in member_codes]

        absorb = dict(eod.get("absorb") or {})
        if absorb.get("available"):
            absorb["inflow"] = pick(absorb.get("inflow") or [])
            absorb["outflow"] = pick(absorb.get("outflow") or [])

        offmarket = dict(eod.get("offmarket") or {})
        if offmarket.get("available"):
            for key in ("north_top10", "blocks", "top_inst"):
                offmarket[key] = pick(offmarket.get(key) or [])

        if em_section.get("available"):
            em_section["inflow"] = pick(em_section.get("inflow") or [])
            em_section["outflow"] = pick(em_section.get("outflow") or [])

        return {
            "as_of": china_now().strftime("%H:%M:%S"),
            "session": market_session(),
            "enabled": bool(self.settings.dark_pool_enabled),
            "market": self._market_strip(eod, em_section),
            "absorb": absorb,
            "offmarket": offmarket,
            "em": em_section,
            "sector_filter": sector_filter,
        }

    def stock_payload(self, code: str) -> dict[str, Any]:
        """个股暗盘资金摘要：详情页右栏使用。EOD 存储 + 东财快照，零行情请求。"""
        code = str(code or "").strip().zfill(6)
        if not code.isdigit():
            return {"available": False, "note": "无效代码"}
        names = self._name_map()
        payload: dict[str, Any] = {
            "available": True,
            "code": code,
            "name": names.get(code, code),
            "as_of": china_now().strftime("%H:%M:%S"),
        }
        payload.update(self._stock_eod(code))
        em_row = self._em_row(code)
        if em_row:
            payload["em"] = em_row
        return payload

    # ------------------------------------------------------------------
    # 板块联动：把首页选中的板块解析成成员代码集合
    # ------------------------------------------------------------------
    def _resolve_sector_filter(self, sector: str | None, board_level: int) -> dict[str, Any] | None:
        sector = str(sector or "").strip()
        if not sector:
            return None
        info: dict[str, Any] = {"sector": sector, "board_level": board_level, "member_count": 0, "member_codes": []}
        if not callable(self._sector_members_provider):
            return info
        try:
            members_by_sector = self._sector_members_provider(board_level) or {}
        except Exception:  # noqa: BLE001
            return info
        codes = members_by_sector.get(sector) or []
        info["member_codes"] = list(codes)
        info["member_count"] = len(codes)
        return info

    # ------------------------------------------------------------------
    # 盘中资金地图：东财快照 + easy_tdx 板块归组
    # ------------------------------------------------------------------
    def _em_section(self) -> dict[str, Any]:
        if self._em is None:
            return {"available": False, "note": "东财源未接线"}
        snap = self._em.snapshot()
        if not snap.get("available"):
            return {"available": False, "note": snap.get("note") or "东财资金流不可用"}
        snap_rows = snap.get("rows")
        rows = list(snap_rows.values()) if isinstance(snap_rows, dict) else list(snap_rows or [])
        sector_maps = self._sector_maps_all_levels()
        sectors_l3 = sector_maps.get(3) or {}

        def view(row: dict[str, Any]) -> dict[str, Any]:
            return {
                "code": row["code"],
                "name": row["name"],
                "sector": sectors_l3.get(row["code"], ""),
                "change_pct": round(float(row.get("change_pct") or 0), 2),
                "main_net": round(float(row.get("main_net") or 0), 0),
                "main_pct": round(float(row.get("main_pct") or 0), 1),
                "elg_net": round(float(row.get("elg_net") or 0), 0),
            }

        inflow = [view(r) for r in sorted(rows, key=lambda r: float(r.get("main_net") or 0), reverse=True)[:15] if float(r.get("main_net") or 0) > 0]
        outflow = [view(r) for r in sorted(rows, key=lambda r: float(r.get("main_net") or 0))[:10] if float(r.get("main_net") or 0) < 0]
        total_main = sum(float(r.get("main_net") or 0) for r in rows)
        return {
            "available": True,
            "as_of": snap.get("as_of") or "",
            "stock_count": snap.get("stock_count") or len(rows),
            "total_main_net": round(total_main, 0),
            "stale_error": snap.get("stale_error") or "",
            "source": "东财 push2 盘中资金流（按单笔金额分桶的推断口径，非隐藏单真值）",
            "inflow": inflow,
            "outflow": outflow,
            "sector_rollup_by_level": {
                f"l{level}": self._sector_rollup(
                    [dict(view(r), sector=(sector_maps.get(level) or {}).get(r["code"], "")) for r in rows],
                    "main_net",
                )
                for level in (1, 2, 3)
            },
        }

    def _em_row(self, code: str) -> dict[str, Any] | None:
        if self._em is None:
            return None
        snap = self._em.snapshot()
        rows = snap.get("rows")
        if not snap.get("available") or not isinstance(rows, dict):
            return None
        row = rows.get(code)
        if not row:
            return None
        return {
            "as_of": snap.get("as_of") or "",
            "main_net": round(float(row.get("main_net") or 0), 0),
            "main_pct": round(float(row.get("main_pct") or 0), 1),
            "elg_net": round(float(row.get("elg_net") or 0), 0),
            "lg_net": round(float(row.get("lg_net") or 0), 0),
        }

    # ------------------------------------------------------------------
    # 背景条
    # ------------------------------------------------------------------
    def _market_strip(self, eod: dict[str, Any], em_section: dict[str, Any]) -> dict[str, Any]:
        market = dict(eod.get("market") or {})
        if em_section.get("available"):
            market["em_main_net"] = em_section.get("total_main_net")
            market["em_as_of"] = em_section.get("as_of")
        market["available"] = bool(market)
        return market

    # ------------------------------------------------------------------
    # 官方口径：EOD 访问层（300s TTL）
    # ------------------------------------------------------------------
    def _eod_payload(self) -> dict[str, Any]:
        now = time.monotonic()
        cache = self._eod_cache
        if cache and now - cache[0] < EOD_CACHE_TTL_SECONDS:
            return dict(cache[1])
        payload = self._load_eod()
        self._eod_cache = (now, payload)
        return dict(payload)

    def _t(self, logical: str) -> str:
        return self._store.table(logical)

    def _query(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return self._store.query(sql, args)

    def _latest_date(self, table: str) -> str:
        return self._store.latest_date(table)

    def _attach_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """快照模式：快照只存代码与数值，名称/板块用本容器 easy_tdx 映射回填。"""
        if not payload.get("available"):
            return payload
        names = self._name_map()
        sectors = self._sector_map(3)

        def fill(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            for row in rows:
                code = str(row.get("code") or "")
                row["name"] = names.get(code, code)
                row["sector"] = sectors.get(code, "")
            return rows

        absorb = dict(payload.get("absorb") or {})
        absorb["inflow"] = fill(list(absorb.get("inflow") or []))
        absorb["outflow"] = fill(list(absorb.get("outflow") or []))
        payload["absorb"] = absorb
        offmarket = dict(payload.get("offmarket") or {})
        for key in ("north_top10", "blocks", "top_inst"):
            offmarket[key] = fill(list(offmarket.get(key) or []))
        payload["offmarket"] = offmarket
        return payload

    def _load_eod(self) -> dict[str, Any]:
        if getattr(self._store, "backend", "") == "cloudbase_snapshot":
            return self._attach_identity(self._store.eod_payload())  # type: ignore[attr-defined]
        if not self._store.available:
            return {"available": False, "note": "EOD 存储未配置（pymysql 或 db_config 缺失）"}
        try:
            trade_date = self._latest_date("moneyflow")
            if not trade_date:
                return {"available": False, "note": "MySQL 暂无 moneyflow 数据：跑 scripts/ingest_eod_tushare.py 落库"}
            names = self._name_map()
            sectors = self._sector_map(3)
            return {
                "available": True,
                "trade_date": trade_date,
                "market": self._load_market(trade_date),
                "absorb": self._load_absorb(names, sectors),
                "offmarket": self._load_offmarket(names, sectors),
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("dark pool eod load failed: %s", exc)
            return {"available": False, "note": f"读取 EOD 存储失败：{exc}"}

    def _load_market(self, trade_date: str) -> dict[str, Any]:
        market: dict[str, Any] = {"trade_date": trade_date}
        row = self._query(f"SELECT SUM(net_mf_amount) AS v FROM {self._t('moneyflow')} WHERE trade_date = %s", (trade_date,))
        if row and row[0]["v"] is not None:
            # moneyflow 金额单位万元 → 元
            market["main_net_amount"] = round(float(row[0]["v"]) * 10000, 0)

        hsgt_date = self._latest_date("moneyflow_hsgt")
        if hsgt_date:
            row = self._query(f"SELECT north_money FROM {self._t('moneyflow_hsgt')} WHERE trade_date = %s", (hsgt_date,))
            try:
                # 百万元 → 元；2024-08 起北向只披露成交额，无净额口径
                market["north_turnover"] = round(float(row[0]["north_money"]) * 1e6, 0) if row and row[0]["north_money"] is not None else None
                market["north_trade_date"] = hsgt_date
            except (TypeError, ValueError):
                pass

        margin_dates = [str(r["d"]) for r in self._query(f"SELECT DISTINCT trade_date AS d FROM {self._t('margin_detail')} ORDER BY d DESC LIMIT 2")]
        if margin_dates:
            def rzye(d: str) -> float:
                row = self._query(f"SELECT SUM(rzye) AS v FROM {self._t('margin_detail')} WHERE trade_date = %s", (d,))
                return float(row[0]["v"] or 0) if row else 0.0

            latest = rzye(margin_dates[0])
            market["margin_trade_date"] = margin_dates[0]
            market["margin_balance"] = round(latest, 0)
            if len(margin_dates) > 1:
                market["margin_change"] = round(latest - rzye(margin_dates[1]), 0)

        row = self._query(f"SELECT SUM(amount) AS v FROM {self._t('block_trade')} WHERE trade_date = %s", (trade_date,))
        if row and row[0]["v"] is not None:
            market["block_amount"] = round(float(row[0]["v"]) * 10000, 0)
        return market

    def _load_absorb(self, names: dict[str, str], sectors: dict[str, str]) -> dict[str, Any]:
        """暗吸/暗派：多日连续同向净额 + 价格滞涨/抗跌。"""
        dates = [
            str(r["d"])
            for r in self._query(
                f"SELECT DISTINCT trade_date AS d FROM {self._t('moneyflow')} ORDER BY d DESC LIMIT {int(ABSORB_WINDOW_DAYS)}"
            )
        ]
        if len(dates) < ABSORB_MIN_DAYS:
            return {"available": False, "note": f"历史不足（{len(dates)}/{ABSORB_WINDOW_DAYS} 天），跑 scripts/ingest_eod_tushare.py --days 回填"}
        dates = sorted(dates)
        placeholders = ",".join(["%s"] * len(dates))
        rows = self._query(
            f"SELECT m.ts_code, m.trade_date, m.net_mf_amount, d.close, d.turnover_rate"
            f" FROM {self._t('moneyflow')} m LEFT JOIN {self._t('daily_basic')} d"
            "   ON m.ts_code = d.ts_code AND m.trade_date = d.trade_date"
            f" WHERE m.trade_date IN ({placeholders})",
            tuple(dates),
        )
        by_code: dict[str, list[tuple[str, float, float, float]]] = {}
        for r in rows:
            by_code.setdefault(str(r["ts_code"]), []).append(
                (
                    str(r["trade_date"]),
                    float(r["net_mf_amount"] or 0) * 10000,  # 万元 → 元
                    float(r["close"] or 0),
                    float(r["turnover_rate"] or 0),
                )
            )

        inflow: list[dict[str, Any]] = []
        outflow: list[dict[str, Any]] = []
        for ts_code, items in by_code.items():
            items.sort(key=lambda x: x[0])
            net_total = sum(x[1] for x in items)
            pos_days = sum(1 for x in items if x[1] > 0)
            neg_days = sum(1 for x in items if x[1] < 0)
            closes = [x[2] for x in items if x[2] > 0]
            chg_pct = (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 2 else 0.0
            if abs(chg_pct) > ABSORB_MAX_PRICE_PCT:
                continue
            if abs(net_total) < ABSORB_MIN_NET:
                continue
            turnover_vals = [x[3] for x in items if x[3] > 0]
            code = ts_code[:6]
            row = {
                "code": code,
                "name": names.get(code, code),
                "sector": sectors.get(code, ""),
                "net_window": round(net_total, 0),
                "pos_days": pos_days,
                "neg_days": neg_days,
                "days": len(items),
                "window_chg_pct": round(chg_pct, 2),
                "turnover_avg": round(sum(turnover_vals) / len(turnover_vals), 2) if turnover_vals else 0.0,
                "close": round(closes[-1], 2) if closes else 0.0,
            }
            if net_total > 0 and pos_days >= ABSORB_MIN_DAYS:
                inflow.append(row)
            elif net_total < 0 and neg_days >= ABSORB_MIN_DAYS:
                outflow.append(row)
        inflow.sort(key=lambda r: r["net_window"], reverse=True)
        outflow.sort(key=lambda r: r["net_window"])
        return {
            "available": bool(inflow or outflow),
            "window_dates": dates,
            "window_days": len(dates),
            "rule": f"近{len(dates)}日同向净额≥{ABSORB_MIN_DAYS}天 且 区间涨跌幅|≤{ABSORB_MAX_PRICE_PCT}%|",
            "source": "Tushare moneyflow 多日窗口 × daily_basic 收盘价（推断口径）",
            "inflow": inflow[:12],
            "outflow": outflow[:12],
        }

    def _load_offmarket(self, names: dict[str, str], sectors: dict[str, str]) -> dict[str, Any]:
        """大手场外：北向十大成交 + 大宗交易 + 龙虎榜机构席位。"""
        trade_date = self._latest_date("moneyflow")

        north_date = self._latest_date("hsgt_top10")
        north_top10: list[dict[str, Any]] = []
        if north_date:
            for r in self._query(
                f"SELECT ts_code, name, close, `change`, amount FROM {self._t('hsgt_top10')}"
                " WHERE trade_date = %s ORDER BY amount DESC LIMIT 10",
                (north_date,),
            ):
                code = str(r["ts_code"])[:6]
                north_top10.append(
                    {
                        "code": code,
                        "name": names.get(code, str(r["name"] or code)),
                        "sector": sectors.get(code, ""),
                        "change_pct": round(float(r["change"] or 0), 2),
                        "amount": round(float(r["amount"] or 0), 0),  # 单位元；仅成交额无方向
                    }
                )

        blocks: list[dict[str, Any]] = []
        if trade_date:
            top_list_codes = {
                str(r["ts_code"])
                for r in self._query(f"SELECT DISTINCT ts_code FROM {self._t('top_list')} WHERE trade_date = %s", (trade_date,))
            }
            for r in self._query(
                f"SELECT b.ts_code, b.price, b.vol, b.amount, d.close,"
                "       ROUND((b.price / d.close - 1) * 100, 2) AS premium_pct"
                f" FROM {self._t('block_trade')} b JOIN {self._t('daily_basic')} d"
                "   ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date"
                " WHERE b.trade_date = %s ORDER BY b.amount DESC LIMIT 10",
                (trade_date,),
            ):
                code = str(r["ts_code"])[:6]
                blocks.append(
                    {
                        "code": code,
                        "name": names.get(code, code),
                        "sector": sectors.get(code, ""),
                        "price": float(r["price"] or 0),
                        "close": float(r["close"] or 0),
                        "amount": round(float(r["amount"] or 0) * 10000, 0),  # 万元 → 元
                        "premium_pct": float(r["premium_pct"] or 0),
                        "on_top_list": str(r["ts_code"]) in top_list_codes,
                    }
                )

        inst_date = self._latest_date("top_inst")
        top_inst: list[dict[str, Any]] = []
        if inst_date:
            for r in self._query(
                f"SELECT ts_code,"
                "       SUM(CASE WHEN exalter LIKE '%%机构%%' THEN net_buy ELSE 0 END) AS inst_net,"
                "       SUM(net_buy) AS total_net, COUNT(*) AS seats"
                f" FROM {self._t('top_inst')} WHERE trade_date = %s"
                " GROUP BY ts_code HAVING inst_net != 0"
                " ORDER BY ABS(inst_net) DESC LIMIT 10",
                (inst_date,),
            ):
                code = str(r["ts_code"])[:6]
                top_inst.append(
                    {
                        "code": code,
                        "name": names.get(code, code),
                        "sector": sectors.get(code, ""),
                        "inst_net": round(float(r["inst_net"] or 0), 0),  # 单位元
                        "total_net": round(float(r["total_net"] or 0), 0),
                        "seats": int(r["seats"] or 0),
                    }
                )

        return {
            "available": bool(north_top10 or blocks or top_inst),
            "trade_date": trade_date,
            "north_trade_date": north_date,
            "inst_trade_date": inst_date,
            "north_note": "北向十大成交仅成交额：2024-08 起交易所不再披露北向个股买卖方向",
            "north_top10": north_top10,
            "blocks": blocks,
            "top_inst": top_inst,
        }

    # ------------------------------------------------------------------
    # 个股摘要（详情页）
    # ------------------------------------------------------------------
    def _stock_eod(self, code: str) -> dict[str, Any]:
        if getattr(self._store, "backend", "") == "cloudbase_snapshot":
            summary = self._store.stock_summary(code)  # type: ignore[attr-defined]
            if summary:
                out = {"eod_available": True}
                out.update(summary)
                return out
            # 快照未覆盖：登记补数请求，本地履约管线推送单票摘要后前端轮询自取
            requested = False
            request_fn = getattr(self._store, "request_stock", None)
            if callable(request_fn):
                try:
                    requested = bool(request_fn(code))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("暗盘补数请求登记失败 %s: %s", code, exc)
            if requested:
                return {
                    "eod_available": False,
                    "pending": True,
                    "note": "快照未覆盖该票，已提交补数请求：本地管线履约后自动加载",
                }
            return {"eod_available": False, "note": "生产快照未覆盖该票（快照预计算范围为面板+自选/持仓）"}
        if not self._store.available:
            return {"eod_available": False, "note": "EOD 存储未配置（pymysql 或 db_config 缺失）"}
        ts_code = self._to_ts_code(code)
        out: dict[str, Any] = {"eod_available": True}
        try:
            flow = [
                {
                    "trade_date": str(r["trade_date"]),
                    "net": round(float(r["net_mf_amount"] or 0) * 10000, 0),
                    "close": float(r["close"] or 0),
                    "turnover": float(r["turnover_rate"] or 0),
                }
                for r in self._query(
                    f"SELECT m.trade_date, m.net_mf_amount, d.close, d.turnover_rate"
                    f" FROM {self._t('moneyflow')} m LEFT JOIN {self._t('daily_basic')} d"
                    "   ON m.ts_code = d.ts_code AND m.trade_date = d.trade_date"
                    " WHERE m.ts_code = %s ORDER BY m.trade_date DESC LIMIT 10",
                    (ts_code,),
                )
            ]
            flow.reverse()
            out["flow_10d"] = flow
            out["trade_date"] = flow[-1]["trade_date"] if flow else ""

            window = flow[-ABSORB_WINDOW_DAYS:]
            if window:
                net_total = sum(x["net"] for x in window)
                pos_days = sum(1 for x in window if x["net"] > 0)
                neg_days = sum(1 for x in window if x["net"] < 0)
                closes = [x["close"] for x in window if x["close"] > 0]
                chg = (closes[-1] / closes[0] - 1) * 100 if len(closes) >= 2 else 0.0
                flat = abs(chg) <= ABSORB_MAX_PRICE_PCT
                if net_total > 0 and pos_days >= ABSORB_MIN_DAYS and flat:
                    verdict = "疑似暗吸"
                elif net_total < 0 and neg_days >= ABSORB_MIN_DAYS and flat:
                    verdict = "疑似暗派"
                elif net_total > 0:
                    verdict = "主力净入"
                elif net_total < 0:
                    verdict = "主力净出"
                else:
                    verdict = "均衡"
                out["verdict"] = {
                    "label": verdict,
                    "net_window": round(net_total, 0),
                    "pos_days": pos_days,
                    "neg_days": neg_days,
                    "days": len(window),
                    "window_chg_pct": round(chg, 2),
                }

            ths = self._query(
                f"SELECT net_amount, net_d5_amount FROM {self._t('moneyflow_ths')} WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 1",
                (ts_code,),
            )
            if ths:
                out["ths"] = {
                    "net_today": round(float(ths[0]["net_amount"] or 0) * 10000, 0),
                    "net_d5": round(float(ths[0]["net_d5_amount"] or 0) * 10000, 0),
                }
            dc = self._query(
                f"SELECT net_amount FROM {self._t('moneyflow_dc')} WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 1",
                (ts_code,),
            )
            if dc:
                out["dc"] = {"net_today": round(float(dc[0]["net_amount"] or 0) * 10000, 0)}

            north = self._query(
                f"SELECT trade_date, amount FROM {self._t('hsgt_top10')} WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 1",
                (ts_code,),
            )
            if north:
                out["north_top10"] = {"trade_date": str(north[0]["trade_date"]), "amount": round(float(north[0]["amount"] or 0), 0)}

            blocks = self._query(
                f"SELECT b.trade_date, b.price, b.amount, d.close,"
                "       ROUND((b.price / d.close - 1) * 100, 2) AS premium_pct"
                f" FROM {self._t('block_trade')} b JOIN {self._t('daily_basic')} d"
                "   ON b.ts_code = d.ts_code AND b.trade_date = d.trade_date"
                " WHERE b.ts_code = %s ORDER BY b.trade_date DESC LIMIT 5",
                (ts_code,),
            )
            out["blocks"] = [
                {
                    "trade_date": str(r["trade_date"]),
                    "price": float(r["price"] or 0),
                    "close": float(r["close"] or 0),
                    "amount": round(float(r["amount"] or 0) * 10000, 0),
                    "premium_pct": float(r["premium_pct"] or 0),
                }
                for r in blocks
            ]

            margin_rows = self._query(
                f"SELECT trade_date, rzye, rzmre FROM {self._t('margin_detail')} WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 2",
                (ts_code,),
            )
            if margin_rows:
                latest = {"trade_date": str(margin_rows[0]["trade_date"]), "rzye": round(float(margin_rows[0]["rzye"] or 0), 0)}
                if len(margin_rows) > 1:
                    latest["rzye_change"] = round(float(margin_rows[0]["rzye"] or 0) - float(margin_rows[1]["rzye"] or 0), 0)
                out["margin"] = latest

            top_hits = self._query(
                f"SELECT trade_date, reason FROM {self._t('top_list')} WHERE ts_code = %s ORDER BY trade_date DESC LIMIT 3",
                (ts_code,),
            )
            out["top_list"] = [
                {"trade_date": str(r["trade_date"]), "reason": str(r["reason"] or "")}
                for r in top_hits
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("dark pool stock eod failed for %s: %s", code, exc)
            out["eod_available"] = False
            out["note"] = f"读取 EOD 存储失败：{exc}"
        return out

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------
    def _sector_map(self, level: int = 3) -> dict[str, str]:
        if not callable(self._sector_mapper):
            return {}
        try:
            return self._sector_mapper(level) or {}
        except TypeError:
            try:
                return self._sector_mapper() or {}  # type: ignore[call-arg]
            except Exception:  # noqa: BLE001
                return {}
        except Exception:  # noqa: BLE001
            return {}

    def _sector_maps_all_levels(self) -> dict[int, dict[str, str]]:
        return {level: self._sector_map(level) for level in (1, 2, 3)}

    @staticmethod
    def _sector_rollup(rows: list[dict[str, Any]], amount_key: str) -> list[dict[str, Any]]:
        """按板块汇总净额：板块内个股求和，净流入 top8 + 净流出 top4。"""
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            sector = str(row.get("sector") or "").strip() or "未分类"
            bucket = grouped.setdefault(sector, {"sector": sector, "net_amount": 0.0, "stock_count": 0, "top_name": "", "top_net": 0.0})
            net = float(row.get(amount_key) or 0)
            bucket["net_amount"] += net
            bucket["stock_count"] += 1
            if abs(net) > abs(bucket["top_net"]):
                bucket["top_net"] = net
                bucket["top_name"] = str(row.get("name") or row.get("code") or "")
        result = sorted(grouped.values(), key=lambda b: b["net_amount"], reverse=True)
        picked = [b for b in result if b["net_amount"] > 0][:8] + [b for b in reversed(result) if b["net_amount"] < 0][:4]
        picked.sort(key=lambda b: abs(b["net_amount"]), reverse=True)
        for bucket in picked:
            bucket["net_amount"] = round(bucket["net_amount"], 0)
            bucket["top_net"] = round(bucket["top_net"], 0)
        return picked

    @staticmethod
    def _to_ts_code(code: str) -> str:
        if code.startswith(("6", "9")):
            return f"{code}.SH"
        if code.startswith(("4", "8")):
            return f"{code}.BJ"
        return f"{code}.SZ"

    def _name_map(self) -> dict[str, str]:
        try:
            context = self._context_provider()
        except Exception:  # noqa: BLE001
            return {}
        quotes = getattr(getattr(context, "snapshot", None), "quotes", []) or []
        return {q.code: q.name for q in quotes if q.code}

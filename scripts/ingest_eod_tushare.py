"""Tushare 收盘数据管线：拉取 EOD 数据落本地 MySQL（默认 watchtower_eod 库）。

数据源边界（与 AGENTS.md 一致）：本管线只做收盘后批量落库，盘中实时
行情与 L1 磁带仍走 easy_tdx，二者不混用。

存储（2026-08-18 起 sqlite → MySQL 统一，见 app/eod_store.py）：
  本地：pymysql 直连，库表 eod_*，连接配置取 ts2db_config.yaml 的 db_config
        （或 WATCH_EOD_MYSQL_* 环境变量）。
  生产：云托管容器访问不到本地库，用 --push-prod 把暗盘快照预计算后推送
        CloudBase NoSQL，生产暗盘模块读快照（WATCH_EOD_STORE_BACKEND=cloudbase_snapshot）。

默认数据集（5000 积分全部覆盖，已实测）：
  moneyflow / block_trade / top_list / top_inst / margin_detail /
  moneyflow_hsgt / daily_basic / limit_list_d /
  hsgt_top10 / hk_hold / moneyflow_dc / moneyflow_ths

用法：
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py                 # 最近已收盘交易日
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py --days 45       # 回填近 45 个自然日内的已收盘交易日
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py --date 20260812 # 指定交易日
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py --only moneyflow,block_trade
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py --push-prod     # 落库后推送生产暗盘快照

重复跑同一天是幂等的：先按 trade_date 删除旧行再写入。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.eod_store import SNAPSHOT_KEY, SNAPSHOT_NAMESPACE, TABLE_PREFIX  # noqa: E402

# 盘中 15:00 收盘，给行情落地留 30 分钟余量；当日 15:30 之后才允许把"今天"当作已收盘。
CLOSE_SETTLE_HHMM = 1530

DATASETS: dict[str, dict] = {
    "moneyflow": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "个股资金流向",
    },
    "block_trade": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "大宗交易",
    },
    "top_list": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "龙虎榜",
    },
    "top_inst": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "龙虎榜机构席位",
    },
    "margin_detail": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "融资融券明细",
    },
    "moneyflow_hsgt": {
        "kwargs": lambda d: {"start_date": d, "end_date": d},
        "date_col": "trade_date",
        "desc": "沪深港通资金流向",
    },
    "daily_basic": {
        "kwargs": lambda d: {
            "trade_date": d,
            "fields": "ts_code,trade_date,close,turnover_rate,turnover_rate_f,"
                      "volume_ratio,pe,pe_ttm,pb,total_share,float_share,"
                      "total_mv,circ_mv",
        },
        "date_col": "trade_date",
        "desc": "每日指标",
    },
    "limit_list_d": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "涨跌停统计",
    },
    # ---- 暗盘资金扩展（2026-08-18 实测 5000 积分覆盖）----
    "hsgt_top10": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "沪深股通十大成交",
    },
    "hk_hold": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "沪深股通持股",
    },
    "moneyflow_dc": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "东财口径资金流向",
    },
    "moneyflow_ths": {
        "kwargs": lambda d: {"trade_date": d},
        "date_col": "trade_date",
        "desc": "同花顺口径资金流向",
    },
}

CALL_SLEEP_SECONDS = 0.2  # 5000 积分 = 500 次/分钟，这里远低于限额
INSERT_CHUNK = 500


def load_token() -> str:
    token = str(settings.secret_config.get("tushare_token") or "").strip()
    if not token:
        raise SystemExit(f"未在 {settings.config_file} 找到 tushare_token")
    return token


def connect(db: str | None = None):
    import pymysql

    cfg = settings.eod_db_config
    kwargs = {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "password": cfg["pwd"],
        "charset": "utf8mb4",
        "autocommit": False,
    }
    if db:
        kwargs["database"] = db
    return pymysql.connect(**kwargs)


def open_trade_dates(pro: ts.pro_api, days: int) -> list[str]:
    today = datetime.now()
    end = today.strftime("%Y%m%d")
    start = (today - timedelta(days=max(10, days))).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    dates = sorted(str(v) for v in cal["cal_date"].tolist())
    if not dates:
        raise SystemExit("区间内没有交易日，请检查 trade_cal")
    today_str = today.strftime("%Y%m%d")
    hhmm = int(today.strftime("%H%M"))
    return [d for d in dates if d < today_str or (d == today_str and hhmm >= CLOSE_SETTLE_HHMM)]


def column_type(df: pd.DataFrame, col: str) -> str:
    if col == "ts_code":
        return "VARCHAR(20)"
    if col == "trade_date":
        return "DATE"
    series = df[col]
    if pd.api.types.is_bool_dtype(series) or pd.api.types.is_float_dtype(series):
        return "DOUBLE"
    if pd.api.types.is_integer_dtype(series):
        return "BIGINT"
    return "TEXT"


def ensure_table(conn, table: str, df: pd.DataFrame) -> list[str]:
    """建表（不存在时）+ 补缺列；返回最终列顺序。conn 为 pymysql 游标。"""
    cols = [str(c) for c in df.columns]
    conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables"
        " WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    exists = conn.fetchone()[0]
    if not exists:
        defs = ", ".join(f"`{c}` {column_type(df, c)}" for c in cols)
        keys = ["KEY `idx_trade_date` (`trade_date`)"] if "trade_date" in cols else []
        if "ts_code" in cols and "trade_date" in cols:
            keys.append("KEY `idx_code_date` (`ts_code`, `trade_date`)")
        ddl = f"CREATE TABLE `{table}` ({defs}{', ' if keys else ''}{', '.join(keys)}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        conn.execute(ddl)
        return cols
    conn.execute(f"SHOW COLUMNS FROM `{table}`")
    existing = {row[0] for row in conn.fetchall()}
    for col in cols:
        if col not in existing:
            conn.execute(f"ALTER TABLE `{table}` ADD COLUMN `{col}` {column_type(df, col)}")
    return cols


def upsert(conn, table: str, df: pd.DataFrame, date_col: str, trade_date: str) -> int:
    if df is None or df.empty:
        # 幂等语义保持一致：空帧也清掉当日旧行（表可能尚未建立，跳过）。
        try:
            conn.execute(f"DELETE FROM `{table}` WHERE `{date_col}` = %s", (trade_date,))
        except Exception:  # noqa: BLE001
            pass
        return 0
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    cols = ensure_table(conn, table, df)
    df = df[cols].astype(object).where(pd.notna(df), None)
    conn.execute(f"DELETE FROM `{table}` WHERE `{date_col}` = %s", (trade_date,))
    placeholders = ", ".join(["%s"] * len(cols))
    col_list = ", ".join(f"`{c}`" for c in cols)
    sql = f"INSERT INTO `{table}` ({col_list}) VALUES ({placeholders})"
    rows = [tuple(row) for row in df.itertuples(index=False, name=None)]
    for i in range(0, len(rows), INSERT_CHUNK):
        conn.executemany(sql, rows[i : i + INSERT_CHUNK])
    return len(rows)


def ingest_dates(pro: ts.pro_api, dates: list[str], names: list[str]) -> int:
    failures: list[str] = []
    conn = connect()
    db_name = settings.eod_db_config["db"]
    with conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{db_name}` DEFAULT CHARSET utf8mb4"
        )
    conn.commit()
    conn.close()
    conn = connect(db_name)
    print(f"db={settings.eod_db_config['host']}:{settings.eod_db_config['port']}/{db_name} dates={','.join(dates)}")
    try:
        with conn.cursor() as cur:
            for trade_date in dates:
                for name in names:
                    spec = DATASETS[name]
                    table = f"{TABLE_PREFIX}{name}"
                    try:
                        df = getattr(pro, name)(**spec["kwargs"](trade_date))
                        rows = upsert(cur, table, df, spec["date_col"], trade_date)
                        conn.commit()
                        print(f"  {trade_date} {name:16s} {spec['desc']:<10s} rows={rows}")
                    except Exception as exc:  # noqa: BLE001 - 单数据集失败不拖垮整批
                        conn.rollback()
                        failures.append(f"{trade_date}:{name}")
                        print(f"  {trade_date} {name:16s} FAIL {type(exc).__name__}: {str(exc)[:160]}")
                    time.sleep(CALL_SLEEP_SECONDS)
    finally:
        conn.close()
    if failures:
        print(f"失败数据集: {failures}")
        return 1
    print("done")
    return 0


# ----------------------------------------------------------------------
# 生产快照：预计算暗盘面板 + 关注个股摘要，推送 CloudBase NoSQL
# ----------------------------------------------------------------------
def _strip_identity(rows: list[dict]) -> list[dict]:
    """去掉 name/sector：生产读侧用容器自己的 easy_tdx 映射回填，快照只存代码与数值。"""
    return [{k: v for k, v in row.items() if k not in ("name", "sector")} for row in rows]


def _snapshot_codes(eod: dict) -> list[str]:
    """需要预计算个股摘要的代码：自选 + 持仓 + 面板已覆盖个股。"""
    codes: set[str] = set()
    for file_name in ("watchlist.json", "positions.json"):
        path = settings.data_dir / file_name
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        except Exception:  # noqa: BLE001
            data = []
        items = data if isinstance(data, list) else data.get("items") or data.get("positions") or []
        for item in items:
            code = str((item or {}).get("code") or "").strip().zfill(6)
            if len(code) == 6 and code.isdigit():
                codes.add(code)
    absorb = eod.get("absorb") or {}
    offmarket = eod.get("offmarket") or {}
    for rows in (
        absorb.get("inflow") or [],
        absorb.get("outflow") or [],
        offmarket.get("north_top10") or [],
        offmarket.get("blocks") or [],
        offmarket.get("top_inst") or [],
    ):
        for row in rows:
            code = str(row.get("code") or "").strip().zfill(6)
            if len(code) == 6 and code.isdigit():
                codes.add(code)
    return sorted(codes)


def push_prod_snapshot() -> int:
    from app.cloud_persistence import CloudBaseNoSqlStateStore
    from app.dark_pool import DarkPoolMonitor
    from app.eod_store import build_eod_store

    if not settings.cloudbase_env_id or not settings.cloudbase_api_token:
        print("缺少 WATCH_CLOUDBASE_ENV_ID / WATCH_CLOUDBASE_API_TOKEN，无法推送快照")
        return 1

    monitor = DarkPoolMonitor(
        settings,
        context_provider=lambda: None,  # 名称/板块由生产读侧回填，快照只存代码
        eod_store=build_eod_store(settings),
    )
    eod = monitor._load_eod()
    if not eod.get("available"):
        print(f"本地 EOD 不可用：{eod.get('note')}")
        return 1
    absorb = dict(eod.get("absorb") or {})
    absorb["inflow"] = _strip_identity(absorb.get("inflow") or [])
    absorb["outflow"] = _strip_identity(absorb.get("outflow") or [])
    offmarket = dict(eod.get("offmarket") or {})
    for key in ("north_top10", "blocks", "top_inst"):
        offmarket[key] = _strip_identity(offmarket.get(key) or [])

    stocks: dict[str, dict] = {}
    for code in _snapshot_codes(eod):
        summary = monitor._stock_eod(code)
        if summary.get("eod_available"):
            stocks[code] = summary

    # 流通市值快照（元）：生产「核心容量」标签口径用，见 services._float_mcap_map
    float_mcap: dict[str, float] = {}
    try:
        store = monitor._store
        latest = store.latest_date("daily_basic")
        if latest:
            for row in store.query(
                f"SELECT ts_code, circ_mv FROM {store.table('daily_basic')} WHERE trade_date = %s",
                (latest,),
            ):
                code = str(row.get("ts_code") or "").split(".")[0].zfill(6)
                try:
                    value = float(row.get("circ_mv")) * 10_000.0  # 万元 → 元
                except (TypeError, ValueError):
                    continue
                if len(code) == 6 and code.isdigit() and value > 0:
                    float_mcap[code] = value
    except Exception as exc:  # noqa: BLE001
        print(f"float_mcap 快照段构建失败（忽略）：{exc}")

    doc = {
        "available": True,
        "trade_date": eod.get("trade_date") or "",
        "pushed_at": datetime.now().isoformat(timespec="seconds"),
        "market": eod.get("market") or {},
        "absorb": absorb,
        "offmarket": offmarket,
        "stocks": stocks,
        "float_mcap": float_mcap,
    }
    store = CloudBaseNoSqlStateStore(
        env_id=settings.cloudbase_env_id,
        token=settings.cloudbase_api_token,
        collection=settings.cloudbase_state_collection,
        instance=settings.cloudbase_database_instance,
        database=settings.cloudbase_database_name,
        base_url=settings.cloudbase_api_base_url or None,
        timeout=settings.cloudbase_api_timeout_seconds,
    )
    store.set_json(SNAPSHOT_NAMESPACE, SNAPSHOT_KEY, doc)
    print(f"快照已推送：trade_date={doc['trade_date']} stocks={len(stocks)}")

    # 顺带履约生产容器登记的按需补数请求（快照未覆盖的个股）
    try:
        from scripts.fulfill_dark_pool_requests import fulfill_once

        fulfill_once()
    except Exception as exc:  # noqa: BLE001
        print(f"补数请求履约失败（忽略，可手动跑 scripts/fulfill_dark_pool_requests.py）：{exc}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Tushare 收盘数据落 MySQL")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD，默认最近已收盘交易日")
    parser.add_argument("--days", type=int, default=0, help="回填近 N 个自然日内的已收盘交易日")
    parser.add_argument("--only", default="", help="只跑指定数据集，逗号分隔")
    parser.add_argument("--push-prod", action="store_true", help="落库后推送生产暗盘快照到 CloudBase NoSQL")
    args = parser.parse_args()

    pro = ts.pro_api(load_token())
    if args.days > 0:
        dates = open_trade_dates(pro, args.days)
    else:
        dates = [args.date.strip()] if args.date.strip() else open_trade_dates(pro, 10)[-1:]
    if not dates or not dates[0]:
        raise SystemExit("最近交易日尚未收盘结算（15:30 后才可拉取当日）")
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(DATASETS)
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        raise SystemExit(f"未知数据集: {unknown}，可选: {list(DATASETS)}")

    rc = ingest_dates(pro, dates, names)
    if rc == 0 and args.push_prod:
        rc = push_prod_snapshot()
    return rc


if __name__ == "__main__":
    sys.exit(main())

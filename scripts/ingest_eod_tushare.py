"""Tushare 收盘数据管线：拉取 EOD 数据落本地 SQLite。

数据源边界（与 AGENTS.md 一致）：本管线只做收盘后批量落库，盘中实时
行情与 L1 磁带仍走 easy_tdx，二者不混用。

默认数据集（5000 积分全部覆盖，已实测）：
  moneyflow       个股资金流向（主力净额 net_mf_amount，大/中/小单）
  block_trade     大宗交易（字面意义的"暗盘"成交）
  top_list        龙虎榜
  top_inst        龙虎榜机构席位
  margin_detail   融资融券明细
  moneyflow_hsgt  沪深港通南北向资金
  daily_basic     每日指标（换手、量比、PE/PB、市值）
  limit_list_d    涨跌停统计（给涨停情绪梯队做 T+1 校验）

用法：
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py                 # 最近已收盘交易日
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py --date 20260812 # 指定交易日
  .\\.venv\\Scripts\\python.exe scripts\\ingest_eod_tushare.py --only moneyflow,block_trade

重复跑同一天是幂等的：先按 trade_date 删除旧行再写入。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import tushare as ts
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT_DIR / "ts2db_config.yaml"
DB_FILE = ROOT_DIR / "data" / "runtime" / "tushare_eod.sqlite"

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
}

CALL_SLEEP_SECONDS = 0.2  # 5000 积分 = 500 次/分钟，这里远低于限额


def load_token() -> str:
    secrets = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    token = str(secrets.get("tushare_token") or "").strip()
    if not token:
        raise SystemExit(f"未在 {CONFIG_FILE} 找到 tushare_token")
    return token


def latest_closed_trade_date(pro: ts.pro_api) -> str:
    today = datetime.now()
    end = today.strftime("%Y%m%d")
    start = (today - timedelta(days=10)).strftime("%Y%m%d")
    cal = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    dates = sorted(str(v) for v in cal["cal_date"].tolist())
    if not dates:
        raise SystemExit("近 10 天没有交易日，请检查 trade_cal")
    today_str = today.strftime("%Y%m%d")
    hhmm = int(today.strftime("%H%M"))
    closed = [d for d in dates if d < today_str or (d == today_str and hhmm >= CLOSE_SETTLE_HHMM)]
    if not closed:
        raise SystemExit(f"最近交易日 {dates[-1]} 尚未收盘结算（15:30 后才可拉取当日）")
    return closed[-1]


def upsert(conn: sqlite3.Connection, table: str, df: pd.DataFrame, date_col: str, trade_date: str) -> int:
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None

    def clear_day() -> None:
        if table_exists:
            conn.execute(f'DELETE FROM "{table}" WHERE "{date_col}" = ?', (trade_date,))

    if df is None or df.empty:
        clear_day()
        return 0
    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    if not table_exists:
        # 首次建表：先用 to_sql 建出结构，再删当日（新表为空，等价 no-op）。
        df.iloc[0:0].to_sql(table, conn, if_exists="replace", index=False)
        table_exists = True
    else:
        # 已有表：新出现的列补 ALTER，保持一致。
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        for col in df.columns:
            if col not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" TEXT')
        existing = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        df = df[[c for c in df.columns if c in existing]]
    clear_day()
    df.to_sql(table, conn, if_exists="append", index=False)
    return len(df)


def main() -> int:
    parser = argparse.ArgumentParser(description="Tushare 收盘数据落库")
    parser.add_argument("--date", default="", help="交易日 YYYYMMDD，默认最近已收盘交易日")
    parser.add_argument("--only", default="", help="只跑指定数据集，逗号分隔")
    parser.add_argument("--db", default=str(DB_FILE), help="SQLite 路径")
    args = parser.parse_args()

    pro = ts.pro_api(load_token())
    trade_date = args.date.strip() or latest_closed_trade_date(pro)
    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(DATASETS)
    unknown = [n for n in names if n not in DATASETS]
    if unknown:
        raise SystemExit(f"未知数据集: {unknown}，可选: {list(DATASETS)}")

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    print(f"trade_date={trade_date} db={db_path}")

    failures: list[str] = []
    for name in names:
        spec = DATASETS[name]
        try:
            df = getattr(pro, name)(**spec["kwargs"](trade_date))
            rows = upsert(conn, name, df, spec["date_col"], trade_date)
            conn.commit()
            print(f"  {name:16s} {spec['desc']:<10s} rows={rows}")
        except Exception as exc:  # noqa: BLE001 - 单数据集失败不拖垮整批
            failures.append(name)
            print(f"  {name:16s} FAIL {type(exc).__name__}: {str(exc)[:160]}")
        time.sleep(CALL_SLEEP_SECONDS)

    conn.close()
    if failures:
        print(f"失败数据集: {failures}")
        return 1
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

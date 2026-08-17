"""暗盘资金候选数据源可用性探测（只读，不落库）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\probe_darkpool_sources.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import tushare as ts
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_token() -> str:
    secrets = yaml.safe_load((ROOT_DIR / "ts2db_config.yaml").read_text(encoding="utf-8")) or {}
    token = str(secrets.get("tushare_token") or "").strip()
    if not token:
        raise SystemExit("未在 ts2db_config.yaml 找到 tushare_token")
    return token


def recent_trade_dates(pro: ts.pro_api, n: int = 5) -> list[str]:
    today = datetime.now()
    cal = pro.trade_cal(
        exchange="SSE",
        start_date=(today - timedelta(days=15)).strftime("%Y%m%d"),
        end_date=today.strftime("%Y%m%d"),
        is_open="1",
    )
    return sorted((str(v) for v in cal["cal_date"].tolist()), reverse=True)[:n]


def probe(pro: ts.pro_api, name: str, dates: list[str]) -> None:
    for d in dates:
        try:
            df = getattr(pro, name)(trade_date=d)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:120]
            print(f"  {name:16s} {d} FAIL {type(exc).__name__}: {msg}")
            return  # 权限类错误换日期也没用，直接停
        if df is not None and not df.empty:
            cols = ",".join(str(c) for c in df.columns[:12])
            print(f"  {name:16s} {d} OK rows={len(df)} cols={cols}")
            return
        print(f"  {name:16s} {d} 空（非交易日或暂无数据），试前一天")
    print(f"  {name:16s} 最近 {len(dates)} 天均无数据")


def main() -> int:
    pro = ts.pro_api(load_token())
    dates = recent_trade_dates(pro)
    print(f"最近交易日: {dates}")
    for name in ["hk_hold", "hsgt_top10", "moneyflow_dc", "moneyflow_ths", "ggt_top10"]:
        probe(pro, name, dates)
    return 0


if __name__ == "__main__":
    sys.exit(main())

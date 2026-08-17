"""暗盘资金重构后 payload 离线探测（不起完整服务，不拉行情）。

用法：
  .\\.venv\\Scripts\\python.exe scripts\\probe_darkpool_payload.py [--code 300308]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from app.config import AppSettings  # noqa: E402
from app.dark_pool import DarkPoolMonitor  # noqa: E402
from app.em_moneyflow import EMMoneyflowCache  # noqa: E402


class _StubContext:
    class snapshot:  # noqa: D106 - 探测桩
        quotes: list = []

    watchlist: list = []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="300308", help="个股摘要探测代码")
    parser.add_argument("--skip-em", action="store_true", help="跳过东财网络拉取")
    args = parser.parse_args()

    settings = AppSettings()
    em = None if args.skip_em else EMMoneyflowCache()
    monitor = DarkPoolMonitor(
        settings,
        context_provider=lambda: _StubContext(),
        sector_mapper=lambda level=3: {},
        sector_members_provider=lambda level=3: {},
        em_cache=em,
    )

    payload = monitor.payload()
    # 东财快照是后台一次性线程补齐（请求路径零阻塞），探测时等它首轮落地
    if em is not None:
        import time

        for _ in range(24):
            if (payload.get("em") or {}).get("available"):
                break
            time.sleep(5)
            payload = monitor.payload()
    market = payload.get("market") or {}
    absorb = payload.get("absorb") or {}
    offmarket = payload.get("offmarket") or {}
    em_sec = payload.get("em") or {}

    print("== market 背景条 ==")
    print(json.dumps(market, ensure_ascii=False, indent=1))
    print("\n== absorb 暗吸/暗派 ==")
    print("available:", absorb.get("available"), "| window:", absorb.get("window_dates"), "|", absorb.get("rule"))
    for row in (absorb.get("inflow") or [])[:5]:
        print(f"  吸 {row['code']} {row['name']} net5d={row['net_window'] / 1e8:+.2f}亿 pos={row['pos_days']}/{row['days']} chg={row['window_chg_pct']:+.2f}% 换手~{row['turnover_avg']}")
    for row in (absorb.get("outflow") or [])[:5]:
        print(f"  派 {row['code']} {row['name']} net5d={row['net_window'] / 1e8:+.2f}亿 neg={row['neg_days']}/{row['days']} chg={row['window_chg_pct']:+.2f}%")
    print("\n== offmarket 大手场外 ==")
    print("available:", offmarket.get("available"), "| north_date:", offmarket.get("north_trade_date"), "| inst_date:", offmarket.get("inst_trade_date"))
    for row in (offmarket.get("north_top10") or [])[:3]:
        print(f"  北向十大 {row['code']} {row['name']} 成交额={row['amount'] / 1e8:.2f}亿")
    for row in (offmarket.get("blocks") or [])[:3]:
        print(f"  大宗 {row['code']} {row['name']} 额={row['amount'] / 1e8:.2f}亿 溢价={row['premium_pct']}%")
    for row in (offmarket.get("top_inst") or [])[:3]:
        print(f"  机构 {row['code']} {row['name']} 机构净买={row['inst_net'] / 1e4:+.0f}万 全部席位={row['total_net'] / 1e4:+.0f}万")
    print("\n== em 盘中资金地图 ==")
    print("available:", em_sec.get("available"), "| as_of:", em_sec.get("as_of"), "| stocks:", em_sec.get("stock_count"), "| total_main_net:", em_sec.get("total_main_net"))
    for row in (em_sec.get("inflow") or [])[:3]:
        print(f"  流入 {row['code']} {row['name']} 主力净={row['main_net'] / 1e8:+.2f}亿 ({row['main_pct']}%)")
    for b in ((em_sec.get("sector_rollup_by_level") or {}).get("l3") or [])[:4]:
        print(f"  板块 {b['sector']} {b['net_amount'] / 1e8:+.2f}亿 ({b['stock_count']}只)")

    print(f"\n== stock_payload({args.code}) ==")
    stock = monitor.stock_payload(args.code)
    verdict = stock.get("verdict") or {}
    print("eod:", stock.get("eod_available"), "| trade_date:", stock.get("trade_date"), "| flow_10d:", len(stock.get("flow_10d") or []), "天")
    print("verdict:", json.dumps(verdict, ensure_ascii=False))
    print("ths:", stock.get("ths"), "| dc:", stock.get("dc"))
    print("north_top10:", stock.get("north_top10"), "| blocks:", len(stock.get("blocks") or []), "| margin:", stock.get("margin"))
    print("top_list:", stock.get("top_list"), "| em:", stock.get("em"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

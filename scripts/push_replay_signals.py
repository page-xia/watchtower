"""用本地服务复算行云科技(300209)当日回放买卖点，并可选推送到飞书 webhook。

用法：
  .venv\\Scripts\\python.exe scripts\\push_replay_signals.py 300209 20260819
  .venv\\Scripts\\python.exe scripts\\push_replay_signals.py 300209 20260819 --webhook https://open.feishu.cn/open-apis/bot/v2/hook/xxx

不带 --webhook 时是 dry-run，只打印会推送的卡片摘要。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import AppSettings  # noqa: E402
from app.models import SignalType, TradeSignal  # noqa: E402
from app.services import DashboardService  # noqa: E402
from app.webhook_push import build_signal_card, _default_sender  # noqa: E402


def marker_to_signal(marker, detail) -> TradeSignal:
    return TradeSignal(
        code=detail.code,
        name=detail.name,
        signal=marker.signal,
        price=marker.price,
        change_pct=marker.change_pct,
        trigger_price=marker.price,
        sector=detail.selected_sector or detail.sector,
        score=marker.score,
        updated_at=marker.time,
        reasons=list(marker.reasons)[:3] or [f"回放复算 · {marker.phase}"],
        phase=marker.phase,
        rebound_from_low_pct=0.0,
        minute_amount_ratio=0.0,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("code", help="股票代码，如 300209")
    parser.add_argument("trade_date", help="交易日 YYYYMMDD，如 20260819")
    parser.add_argument("--webhook", default="", help="飞书机器人 webhook 地址，不传则 dry-run")
    args = parser.parse_args()

    settings = AppSettings()
    service = DashboardService(settings)
    try:
        detail = service.signal_detail(args.code, trade_date=args.trade_date, fast=True)
    finally:
        try:
            service.close()
        except Exception:
            pass

    markers = [
        m for m in (detail.markers or [])
        if m.signal in {SignalType.BUY_T, SignalType.SELL_T}
    ]
    print(f"=== {detail.name}({detail.code}) {detail.trade_date} 回放买卖点 {len(markers)} 个 ===")
    for m in markers:
        print(f"  {m.time}  {m.signal.value}  价 {m.price:.2f} ({m.change_pct:+.2f}%)  "
              f"评分 {m.score}  阶段 {m.phase}  理由 {'；'.join(m.reasons[:2]) or '--'}")

    if not markers:
        print("今日回放没有产生买T/卖T信号，无可推送内容。")
        return 1

    if not args.webhook:
        print("\n[dry-run] 未提供 --webhook，不实际发送。")
        return 0

    sender = _default_sender(timeout_seconds=6.0)
    ok_all = True
    for m in markers:
        card = build_signal_card(marker_to_signal(m, detail))
        ok, info = sender(args.webhook, card)
        ok_all = ok_all and ok
        print(f"  push {m.time} {m.signal.value}: {'OK' if ok else 'FAIL'} ({info})")
    return 0 if ok_all else 2


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-
"""复现板块资金动能「L1 真值锚定」在本地的判定过程。

对照运行时分支：_sector_flow_from_stock_trajectory -> _anchor_series_to_truth
-> _active_net_truth_total(member_codes, quotes_by_code)

输出每个发散板块的：成员数、快照覆盖数、order_flow 可用数、覆盖率是否>=80%、
真值(亿)、与本地展示值的比值是否落在守卫区间 [0.2, 3.0]。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services import DashboardService  # noqa: E402

SECTORS = ["集成电路设计", "半导体材料", "种子", "消费电子组件", "光伏硅料", "半导体封测", "PCB"]

# 本地当前展示值（trajectory 分支 raw/anchored），用于算守卫比值
LOCAL_SHOWN = {
    "集成电路设计": 154.83,
    "半导体材料": 12.13,
    "种子": 0.99,
    "消费电子组件": 50.27,
    "光伏硅料": 2.67,
    "半导体封测": 62.55,
    "PCB": 48.0,
}


def main() -> None:
    service = DashboardService(settings)
    context = service._refresh_context()
    snapshot = context.snapshot
    quotes_by_code = {q.code: q for q in snapshot.quotes if getattr(q, "code", "")}
    print(f"snapshot quotes: {len(quotes_by_code)}  data_mode={snapshot.data_mode}")
    of_avail_total = sum(
        1
        for q in snapshot.quotes
        if getattr(q, "order_flow", None) is not None and q.order_flow.available
    )
    print(f"order_flow available in snapshot: {of_avail_total}/{len(quotes_by_code)}")

    board_context = service.data_source.fetch_board_context(3)
    members_by_sector = service._board_members_by_sector(board_context)

    rows = []
    for name in SECTORS:
        members = members_by_sector.get(name) or []
        covered = 0
        in_snap = 0
        covered_sum = 0.0
        for code in members:
            q = quotes_by_code.get(code)
            if q is None or q.price <= 0:
                continue
            in_snap += 1
            flow = getattr(q, "order_flow", None)
            if flow is None or not getattr(flow, "available", False):
                continue
            bv = float(getattr(flow, "active_buy_volume", 0.0) or 0.0)
            sv = float(getattr(flow, "active_sell_volume", 0.0) or 0.0)
            if bv + sv <= 0:
                continue
            covered += 1
            covered_sum += (bv - sv) * q.price * 100
        need = max(1, int(len(members) * 0.8 + 0.999999))  # ceil(0.8N)
        truth = None
        if members and covered >= need:
            truth = covered_sum / 100_000_000 * len(members) / covered
        shown = LOCAL_SHOWN.get(name)
        ratio = (truth / shown) if (truth is not None and shown) else None
        guard_ok = ratio is not None and 0.2 <= ratio <= 3.0
        rows.append(
            {
                "sector": name,
                "members": len(members),
                "in_snapshot": in_snap,
                "order_flow_ok": covered,
                "need_80pct": need,
                "truth_yi": None if truth is None else round(truth, 2),
                "local_shown": shown,
                "ratio_truth_over_shown": None if ratio is None else round(ratio, 3),
                "anchor_would_fire": bool(truth is not None and guard_ok),
            }
        )
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

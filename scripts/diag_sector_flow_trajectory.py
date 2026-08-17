# -*- coding: utf-8 -*-
"""诊断：用当前代码在进程内走冻结分支轨迹重建，检查锚定是否触发。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.services import DashboardService  # noqa: E402


def main() -> None:
    service = DashboardService(settings)
    context = service._refresh_context()
    snapshot = context.snapshot
    print("data_mode:", snapshot.data_mode, "frozen:", snapshot.source_status.get("frozen"))
    print("quotes:", len(snapshot.quotes))
    of_ok = sum(
        1 for q in snapshot.quotes
        if getattr(q, "order_flow", None) is not None and q.order_flow.available
    )
    print("order_flow available:", of_ok)

    board_context = service.data_source.fetch_board_context(3)
    members = service._board_members_by_sector(board_context)
    sectors = service._official_board_sectors_from_snapshot(context, board_context, members)
    print("sectors built:", len(sectors))

    loader = None  # 用成员映射表做 loader，与终端路径一致
    def member_code_loader(sector):
        return members.get(sector.name) or []

    flow = service._sector_flow_from_trajectory(
        "20260817",
        sectors[:15],
        member_code_loader=member_code_loader,
        quotes=snapshot.quotes,
        state_key="",
    )
    for s in flow:
        print(f"{s.name:<12} final={s.final_value:>8} basis={s.flow_basis}")


if __name__ == "__main__":
    main()

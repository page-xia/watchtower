# 临时冒烟脚本：验证板块资金动能快照代理首屏 + tick 累积 + 分钟线重定基。验证后可删除。
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AppSettings
from app.data_sources import MarketSnapshot
from app.models import Quote, SectorSnapshot
from app.services import DashboardService


def make_quote(code, price, prev_close, amount, volume, minute_amount):
    return Quote(
        code=code,
        name=f"股{code}",
        themes=["PCB"],
        price=price,
        prev_close=prev_close,
        open=prev_close,
        high=price,
        low=prev_close,
        day_high=price,
        day_low=prev_close,
        change_pct=(price - prev_close) / prev_close * 100,
        volume=volume,
        amount=amount,
        minute_amount=minute_amount,
        updated_at="10:00:00",
    )


def make_snapshot(clock_label, amount_a, amount_b, price_b):
    return MarketSnapshot(
        data_mode="live",
        quotes=[
            make_quote("300476", 20.5, 20.0, amount_a, 500000, 8_000_000),
            make_quote("300308", price_b, 30.0, amount_b, 300000, 5_000_000),
        ],
        indices=[],
        source_status={"trade_date": "20260813", "active_source": "easy_tdx", "clock_label": clock_label},
    )


def main() -> None:
    settings = AppSettings()
    service = DashboardService(settings)
    sectors = [
        SectorSnapshot(
            name="PCB",
            heat_score=80,
            avg_change_pct=2.5,
            leader_code="300476",
            leader_name="股300476",
            core_codes=["300476"],
            up_count=2,
            total_count=2,
            limit_up_count=0,
            opened_limit_count=0,
            core_attack=True,
            reasons=["冒烟测试"],
        )
    ]

    # 首屏：无任何分钟线缓存，零网络立即返回代理曲线
    t0 = time.perf_counter()
    first = service._sector_flow_proxy_tick("k1", make_snapshot("10:00:05", 100_000_000, 60_000_000, 30.6), sectors)
    elapsed = (time.perf_counter() - t0) * 1000
    assert first and first[0].points, "首屏代理曲线为空"
    print(f"首屏: {elapsed:.1f}ms, points={[(p.time, p.value) for p in first[0].points]}, basis={first[0].flow_basis}")

    # 同一分钟内第二次 tick：更新末点而不是追加
    second = service._sector_flow_proxy_tick("k1", make_snapshot("10:00:10", 105_000_000, 62_000_000, 30.7), sectors)
    assert len(second[0].points) == len(first[0].points), "同一分钟不应追加点"
    print(f"同分钟更新: points={[(p.time, p.value) for p in second[0].points]}")

    # 跨分钟 tick：追加新点
    third = service._sector_flow_proxy_tick("k1", make_snapshot("10:01:05", 112_000_000, 66_000_000, 30.9), sectors)
    assert len(third[0].points) == len(first[0].points) + 1, "跨分钟应追加一个点"
    print(f"跨分钟追加: points={[(p.time, p.value) for p in third[0].points]}")

    # 模拟分钟线回灌：直接调用 _build_and_cache_sector_flow（fetch_minute_series 打桩）
    # 返回与缓存均为「每分钟净流入」口径（累计曲线已差分）
    minute_rows = [{"price": 20.1 + i * 0.05, "vol": 1000 + i * 10, "amount": 0} for i in range(30)]
    service.data_source.fetch_minute_series = lambda code, trade_date, live=False: list(minute_rows)
    rebuilt = service._build_and_cache_sector_flow("k1", make_snapshot("10:01:05", 112_000_000, 66_000_000, 30.9), sectors)
    assert rebuilt and rebuilt[0].points, "分钟线重建结果为空"
    print(f"分钟线回灌: {len(rebuilt[0].points)} 点, final={rebuilt[0].final_value}, basis={rebuilt[0].flow_basis}")
    assert "每分钟净流入" in rebuilt[0].flow_basis, "回灌结果应为每分钟净流入口径"

    # 差分合并后：历史分钟来自回灌，live 正在成形的分钟保留并继续累积
    fourth = service._sector_flow_proxy_tick("k1", make_snapshot("10:02:05", 120_000_000, 70_000_000, 31.2), sectors)
    labels = [p.time for p in fourth[0].points]
    assert labels[-1] == "10:02", f"最新点应为当前分钟，实际 {labels[-1]}"
    assert "10:00" in labels and "10:01" in labels, "live 历史分钟应保留"
    assert len(labels) == len(set(labels)), "时间标签不应重复"
    print(f"差分合并: {len(labels)} 个点, 最新两点={[(p.time, p.value) for p in fourth[0].points[-2:]]}")

    # 全市场 tick 缓存：快照应并入为逐 code 的分钟级序列
    entry = service._quote_tick_cache.get("300476")
    assert entry and entry["rows"], "tick 缓存未并入快照"
    print(f"tick 缓存: 300476 rows={[(r['time'], r['price'], r['amount']) for r in entry['rows']]}, last={entry['last']}")

    # 冷启动回灌门禁：曲线厚度达标后不再触发分钟线后台重建
    ensure_calls = []
    service._ensure_sector_flow_refresh = lambda *a, **k: ensure_calls.append(a[0] if a else "x")
    snaps = [
        make_snapshot("10:03:05", 122_000_000, 71_000_000, 31.3),
        make_snapshot("10:04:05", 124_000_000, 72_000_000, 31.4),
        make_snapshot("10:05:05", 126_000_000, 73_000_000, 31.5),
        make_snapshot("10:06:05", 128_000_000, 74_000_000, 31.6),
        make_snapshot("10:07:05", 130_000_000, 75_000_000, 31.7),
    ]
    for snap in snaps[:3]:  # 新 namespace：前两次曲线薄（<3 点）触发回灌，第三次起停止
        service._sector_flow_for_context(snap, sectors, cache_namespace="n2", prefer_async=True)
    assert len(ensure_calls) == 2, f"冷启动应只在曲线薄时触发回灌，实际 {len(ensure_calls)} 次"
    for snap in snaps[3:]:
        service._sector_flow_for_context(snap, sectors, cache_namespace="n2", prefer_async=True)
    assert len(ensure_calls) == 2, "曲线厚度达标后不应再触发分钟线回灌"
    print("冷启动门禁: 薄曲线回灌 2 次后停止，厚曲线零请求")
    print("smoke ok")


if __name__ == "__main__":
    main()

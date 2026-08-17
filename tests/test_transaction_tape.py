"""全日逐笔磁带（full_session）的缓存/增量拉取行为。

分页语义（2026-08-17 实测 easy_tdx）：start=0 返回最新一段，页内时间升序，
页面向过去回溯；盘中新成交追加在磁带尾部。盘中刷新应对齐补增量，而不是
整段重拉；历史/收盘后磁带不可变，走长 TTL。
"""

from __future__ import annotations

import sys
import types

from app import data_sources as ds


def _ticks(start_index: int, count: int, price: float = 10.0) -> list[dict]:
    """生成常规时段 tick：时间在 09:31-11:29 内循环（同一分钟多笔是真实常态）。"""
    rows = []
    for i in range(start_index, start_index + count):
        minute = 31 + (i % 118)  # 09:31 .. 11:29
        hh = 9 + minute // 60
        mm = minute % 60
        rows.append(
            {
                "time": f"{hh:02d}:{mm:02d}",
                "price": round(price + (i % 7) * 0.01, 2),
                "vol": 100 + (i % 5) * 10,
                "buyorsell": i % 3,
            }
        )
    return rows


class _FakeTapeClient:
    """模拟 easy_tdx 分页：start=0 为最新一段，页内升序，页向过去回溯。"""

    def __init__(self, tape: list[dict], calls: list[tuple[int, int]]):
        self.tape = tape
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        return None

    def get_transaction_data(self, market, code, start=0, count=1800):  # noqa: ARG002
        self.calls.append((start, count))
        end = len(self.tape) - start
        return [dict(t) for t in self.tape[max(0, end - count):end]]

    def get_history_transaction_data(self, market, code, trade_date, start=0, count=1800):  # noqa: ARG002
        self.calls.append((start, count))
        end = len(self.tape) - start
        return [dict(t) for t in self.tape[max(0, end - count):end]]


def _make_source(monkeypatch, tape: list[dict], calls: list[tuple[int, int]], *, trading: bool):
    settings = ds.AppSettings()
    close_source = ds.EasyTdxDailyDataSource(settings)
    today = ds.china_now().strftime("%Y%m%d")
    monkeypatch.setattr(close_source, "_recent_trade_dates", lambda: [today])
    source = ds.EasyTdxMarketDataSource(settings, close_source)

    client = _FakeTapeClient(tape, calls)
    monkeypatch.setattr(source, "_history_client", lambda: client)
    monkeypatch.setattr(ds, "is_trading_window", lambda: trading)
    return source, today


def _force_stale(source: ds.EasyTdxMarketDataSource, code: str, trade_date: str) -> None:
    """把缓存时间拨回 60s：超过 live TTL（5s）触发刷新，但仍在静态 TTL（1800s）内。"""
    key = (code, trade_date)
    cached_at, observation, ticks = source._transaction_tape_cache[key]
    source._transaction_tape_cache[key] = (cached_at - 60, observation, ticks)


def test_full_session_intraday_incremental_appends_new_ticks(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    tape = _ticks(0, 2500)
    source, today = _make_source(monkeypatch, tape, calls, trading=True)

    first = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    assert first.available is True
    assert first.count == 2500
    # 整段首拉：page0=1800 + page1=700（短页停止）
    assert calls == [(0, 1800), (1800, 1800)]

    # live TTL 内：直接命中缓存，零网络调用
    calls.clear()
    again = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    assert again is first
    assert calls == []

    # 盘中新增 50 笔：过期后只拉最新一页做后缀对齐
    tape.extend(_ticks(2500, 50, price=11.0))  # 键值（价格段不同）与旧磁带不撞车
    _force_stale(source, "300209", today)
    calls.clear()
    updated = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    assert calls == [(0, 1800)]
    assert updated.count == 2550


def test_full_session_static_tape_uses_long_ttl(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    tape = _ticks(0, 900)
    source, today = _make_source(monkeypatch, tape, calls, trading=False)

    first = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    assert first.count == 900
    assert calls == [(0, 1800)]

    # 非盘中（收盘后/历史日）磁带不可变：即使超过 live TTL 也不重拉
    _force_stale(source, "300209", today)
    calls.clear()
    again = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    assert again is first
    assert calls == []


def test_full_session_alignment_failure_falls_back_to_full_refetch(monkeypatch) -> None:
    calls: list[tuple[int, int]] = []
    tape = _ticks(0, 2500)
    source, today = _make_source(monkeypatch, tape, calls, trading=True)

    first = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    assert first.count == 2500

    # 服务端磁带整体改写（对齐不上）→ 回退整段重拉
    tape.clear()
    tape.extend(_ticks(0, 2600, price=20.0))
    _force_stale(source, "300209", today)
    calls.clear()
    refreshed = source.fetch_transaction_flow("300209", trade_date=today, full_session=True)
    # 增量一页 + 整段两页
    assert calls == [(0, 1800), (0, 1800), (1800, 1800)]
    assert refreshed.count == 2600
    assert refreshed.buy_amount > 0 or refreshed.sell_amount > 0

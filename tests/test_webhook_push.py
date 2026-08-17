"""飞书信号推送池：订阅持久化 + 事件签名去重 + 同票 5 分钟限速。"""

from __future__ import annotations

import time

from app.models import SignalType, TradeSignal
from app.webhook_push import (
    SignalPushPool,
    WebhookSubscription,
    WebhookSubscriptionStore,
    build_signal_card,
    validate_feishu_webhook,
)

TEST_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/4766ebf2-02b0-413a-acbe-c216f3a5bb5d"


def make_signal(code: str, *, signal: SignalType = SignalType.BUY_T, updated_at: str = "10:00", price: float = 12.34) -> TradeSignal:
    return TradeSignal(
        code=code,
        name=f"测试{code}",
        signal=signal,
        score=80,
        sector="半导体",
        price=price,
        change_pct=3.21,
        rebound_from_low_pct=1.5,
        minute_amount_ratio=1.8,
        reasons=["量比放大", "指数共振"],
        updated_at=updated_at,
        trigger_price=price,
    )


def make_pool(tmp_path, sent: list, dedup_seconds: float = 300.0) -> SignalPushPool:
    store = WebhookSubscriptionStore(tmp_path / "subs.json")

    def sender(url: str, payload: dict) -> tuple[bool, str]:
        sent.append((url, payload))
        return True, "ok"

    return SignalPushPool(store, dedup_seconds=dedup_seconds, sender=sender)


def subscribe(pool: SignalPushPool, client_id: str, codes: list[str], url: str = TEST_URL, enabled: bool = True) -> None:
    pool.store.upsert(
        WebhookSubscription(client_id=client_id, webhook_url=url, enabled=enabled, codes=codes)
    )


def test_webhook_url_validation() -> None:
    assert validate_feishu_webhook(TEST_URL) == TEST_URL
    for bad in ("", "http://open.feishu.cn/x", "https://example.com/hook/abc"):
        try:
            validate_feishu_webhook(bad)
        except ValueError:
            continue
        raise AssertionError(f"应拒绝非法地址: {bad!r}")


def test_subscription_store_roundtrip(tmp_path) -> None:
    store = WebhookSubscriptionStore(tmp_path / "subs.json")
    store.upsert(WebhookSubscription(client_id="client-a-0001", webhook_url=TEST_URL, enabled=True, codes=["300476", "300476", "1"]))
    loaded = store.get("client-a-0001")
    assert loaded is not None
    assert loaded.enabled is True
    assert loaded.codes == ["300476", "000001"]
    assert loaded.updated_at
    assert store.get("missing-client") is None


def test_push_only_to_enabled_watchers(tmp_path) -> None:
    sent: list = []
    pool = make_pool(tmp_path, sent)
    subscribe(pool, "client-a-0001", ["300476"])
    subscribe(pool, "client-b-0002", ["300476"], enabled=False)  # 关开关不收
    subscribe(pool, "client-c-0003", ["000001"])  # 没自选这只票不收

    pushed = pool.process_signals([make_signal("300476"), make_signal("000001")])
    deadline = time.time() + 3
    while time.time() < deadline and len(sent) < 2:
        time.sleep(0.05)
    urls = sorted(url for url, _ in sent)
    assert pushed == 2
    assert urls == [TEST_URL, TEST_URL]  # 两个事件都只投递给 client-a
    payloads = [payload for _, payload in sent]
    assert all(p["msg_type"] == "interactive" for p in payloads)


def test_same_event_not_repushed(tmp_path) -> None:
    sent: list = []
    pool = make_pool(tmp_path, sent)
    subscribe(pool, "client-a-0001", ["300476"])
    signal = make_signal("300476")
    pool.process_signals([signal])
    pool.process_signals([signal])  # 同一事件签名重复出现
    pool.process_signals([make_signal("300476")])
    deadline = time.time() + 3
    while time.time() < deadline and pool._inflight:
        time.sleep(0.05)
    assert len(sent) == 1


def test_five_minute_rate_limit_per_code(tmp_path) -> None:
    sent: list = []
    pool = make_pool(tmp_path, sent, dedup_seconds=300)
    subscribe(pool, "client-a-0001", ["300476"])
    pool.process_signals([make_signal("300476", updated_at="10:00")])
    # 5 分钟内出现新事件（时间变了）→ 被限速吞掉，且不会稍后补推
    pool.process_signals([make_signal("300476", updated_at="10:03", price=12.5)])
    # 模拟 5 分钟后信号仍在 → 已处理签名相同，不重复推
    pool._last_push_at_by_code["300476"] = time.monotonic() - 301
    pool.process_signals([make_signal("300476", updated_at="10:03", price=12.5)])
    # 5 分钟后出现真正的新事件 → 推
    pool.process_signals([make_signal("300476", updated_at="10:06", price=12.6)])
    deadline = time.time() + 3
    while time.time() < deadline and pool._inflight:
        time.sleep(0.05)
    assert len(sent) == 2


def test_watch_signal_never_pushes(tmp_path) -> None:
    sent: list = []
    pool = make_pool(tmp_path, sent)
    subscribe(pool, "client-a-0001", ["300476"])
    pushed = pool.process_signals([make_signal("300476", signal=SignalType.WATCH)])
    assert pushed == 0
    assert sent == []


def test_card_content() -> None:
    card = build_signal_card(make_signal("300476", signal=SignalType.SELL_T, updated_at="14:00"))
    assert card["msg_type"] == "interactive"
    assert card["card"]["header"]["template"] == "green"  # 卖点=绿
    title = card["card"]["header"]["title"]["content"]
    assert "300476" in title and "卖点" in title

"""飞书 webhook 信号推送：订阅注册、监听池去重、卡片消息投递。

口径约定：
- 每个浏览器客户端用 client_id 注册自己的 webhook 地址 + 开关 + 自选股票池；
- 推送触发只看实盘刷新路径（services._refresh_context）出现的买T/卖T信号，
  「置顶买点/黄金买点」是现价贴近最近买点的展示态，不是信号事件，不触发推送；
- 监听池按票去重：同一代码 5 分钟内（push_signal_dedup_seconds）只推一次，
  同一信号事件（类型+时间+触发价签名）处理过后不再重复推；
- 命中事件后按订阅池逐地址投递，多个订阅同一票时各自收到一张卡片。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field, field_validator

from app.data_sources import china_now
from app.models import SignalType, TradeSignal

logger = logging.getLogger(__name__)

FEISHU_WEBHOOK_PREFIX = "https://open.feishu.cn/open-apis/bot/v2/hook/"

_CODE_RE = re.compile(r"^\d{6}$")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def normalize_stock_code(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text.isdigit() or len(text) > 6:
        return None
    return text.zfill(6)


def validate_feishu_webhook(url: str) -> str:
    text = str(url or "").strip()
    if not text.startswith(FEISHU_WEBHOOK_PREFIX):
        raise ValueError("webhook 地址必须是飞书自定义机器人地址（https://open.feishu.cn/open-apis/bot/v2/hook/...）")
    return text


class WebhookSubscription(BaseModel):
    """一个客户端的推送订阅：webhook 地址 + 开关 + 监听股票池。"""

    client_id: str
    webhook_url: str = ""
    enabled: bool = False
    codes: list[str] = Field(default_factory=list)
    updated_at: str = ""

    @field_validator("client_id")
    @classmethod
    def _check_client_id(cls, value: str) -> str:
        text = str(value or "").strip()
        if not _CLIENT_ID_RE.match(text):
            raise ValueError("client_id 格式不合法")
        return text

    @field_validator("webhook_url")
    @classmethod
    def _check_webhook_url(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return validate_feishu_webhook(text)

    @field_validator("codes", mode="before")
    @classmethod
    def _normalize_codes(cls, value: Any) -> list[str]:
        if not isinstance(value, (list, tuple)):
            return []
        codes: list[str] = []
        seen: set[str] = set()
        for item in value:
            code = normalize_stock_code(item)
            if code and code not in seen:
                seen.add(code)
                codes.append(code)
        return codes[:200]


class WebhookSubscriptionStore:
    """订阅持久化：data/webhook_subscriptions.json，按 client_id 覆盖式更新。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load_all(self) -> dict[str, WebhookSubscription]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        items: dict[str, WebhookSubscription] = {}
        for key, value in raw.items():
            try:
                items[key] = WebhookSubscription.model_validate(value)
            except Exception:
                continue
        return items

    def _save_all(self, items: dict[str, WebhookSubscription]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {key: item.model_dump(mode="json") for key, item in items.items()}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, client_id: str) -> WebhookSubscription | None:
        with self._lock:
            return self._load_all().get(str(client_id or "").strip())

    def upsert(self, item: WebhookSubscription) -> WebhookSubscription:
        item = item.model_copy(update={"updated_at": china_now().isoformat(timespec="seconds")})
        with self._lock:
            items = self._load_all()
            items[item.client_id] = item
            self._save_all(items)
        return item

    def list_all(self) -> list[WebhookSubscription]:
        with self._lock:
            return list(self._load_all().values())


# 投递函数签名：(webhook_url, payload) -> (ok, detail)
PushSender = Callable[[str, dict[str, Any]], tuple[bool, str]]


def _default_sender(timeout_seconds: float) -> PushSender:
    def send(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
        try:
            resp = httpx.post(url, json=payload, timeout=timeout_seconds)
        except Exception as exc:  # 网络异常不能拖垮刷新线程
            return False, f"request_error: {exc}"
        if resp.status_code != 200:
            return False, f"http_{resp.status_code}"
        try:
            body = resp.json()
        except ValueError:
            return False, "invalid_json_response"
        if int(body.get("code", -1)) != 0:
            return False, f"feishu_code_{body.get('code')}: {body.get('msg')}"
        return True, "ok"

    return send


def build_signal_card(signal: TradeSignal) -> dict[str, Any]:
    """买T/卖T 信号的飞书互动卡片（A股惯例：红=买点，绿=卖点）。"""
    is_buy = signal.signal == SignalType.BUY_T
    title = f"{'🔴 买点' if is_buy else '🟢 卖点'}信号 · {signal.name}（{signal.code}）"
    trigger = float(signal.trigger_price or 0) or float(signal.price or 0)
    reasons = "；".join(str(item) for item in (signal.reasons or [])[:3]) or "--"

    def field(label: str, value: str) -> dict[str, Any]:
        return {"is_short": True, "text": {"tag": "lark_md", "content": f"**{label}**\n{value}"}}

    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "red" if is_buy else "green",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        field("信号", f"{signal.signal.value} · {signal.phase or signal.decision_stage or '--'}"),
                        field("现价", f"{float(signal.price or 0):.2f}（{float(signal.change_pct or 0):+.2f}%）"),
                        field("触发价", f"{trigger:.2f}" if trigger > 0 else "--"),
                        field("板块", signal.sector or "--"),
                        field("评分", str(signal.score)),
                        field("时间", signal.updated_at or china_now().strftime("%H:%M")),
                    ],
                },
                {"tag": "div", "text": {"tag": "lark_md", "content": f"**信号理由**\n{reasons}"}},
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": "日内盯盘终端 · 自选股买卖点推送"}],
                },
            ],
        },
    }


def build_test_card(webhook_url: str) -> dict[str, Any]:
    return {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": "✅ 推送测试 · 日内盯盘终端"},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "webhook 连通正常。开启推送后，自选股出现买T/卖T信号时会以卡片消息推送到这里（同一票 5 分钟内只推一次）。",
                    },
                },
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": f"{china_now().isoformat(timespec='seconds')} · {webhook_url[-12:]}",
                        }
                    ],
                },
            ],
        },
    }


class SignalPushPool:
    """信号监听池：全市场信号按票去重后，向订阅了该票的 webhook 统一投递。"""

    def __init__(
        self,
        store: WebhookSubscriptionStore,
        *,
        dedup_seconds: float = 300.0,
        timeout_seconds: float = 6.0,
        sender: PushSender | None = None,
        max_workers: int = 2,
    ) -> None:
        self.store = store
        self.dedup_seconds = max(30.0, float(dedup_seconds))
        self._sender = sender or _default_sender(timeout_seconds)
        self._lock = threading.Lock()
        # code -> 已处理的信号事件签名（类型+时间+触发价），同一事件不重复推
        self._handled_signature_by_code: dict[str, str] = {}
        # code -> 最近一次真实推送的单调时钟，同票 dedup_seconds 内只推一次
        self._last_push_at_by_code: dict[str, float] = {}
        self._max_workers = max(1, int(max_workers))
        self._dispatch_lock = threading.Lock()
        self._inflight = 0

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ pool

    def process_signals(self, signals: list[TradeSignal]) -> int:
        """从一轮实盘信号里挑出新出现的买T/卖T事件并投递，返回实际推送条数。"""
        subscriptions = [
            item for item in self.store.list_all() if item.enabled and item.webhook_url and item.codes
        ]
        if not subscriptions:
            return 0
        watchers_by_code: dict[str, list[WebhookSubscription]] = {}
        for sub in subscriptions:
            for code in sub.codes:
                watchers_by_code.setdefault(code, []).append(sub)

        pushed = 0
        for signal in signals:
            if signal.signal not in {SignalType.BUY_T, SignalType.SELL_T}:
                continue
            code = normalize_stock_code(signal.code)
            if not code:
                continue
            targets = watchers_by_code.get(code)
            if not targets:
                continue
            signature = self._event_signature(signal)
            with self._lock:
                if self._handled_signature_by_code.get(code) == signature:
                    continue
                self._handled_signature_by_code[code] = signature
                now = time.monotonic()
                last_push = self._last_push_at_by_code.get(code, 0.0)
                if now - last_push < self.dedup_seconds:
                    # 同票 5 分钟内只推一次：事件标记为已处理但不投递，稍后不补推
                    continue
                self._last_push_at_by_code[code] = now
            card = build_signal_card(signal)
            for sub in targets:
                self._deliver_async(sub.webhook_url, card)
                pushed += 1
        return pushed

    @staticmethod
    def _event_signature(signal: TradeSignal) -> str:
        trigger = float(signal.trigger_price or 0) or float(signal.price or 0)
        return f"{signal.signal.value}|{signal.updated_at or ''}|{trigger:.3f}"

    # -------------------------------------------------------------- delivery

    def _deliver_async(self, url: str, payload: dict[str, Any]) -> None:
        with self._dispatch_lock:
            if self._inflight >= self._max_workers * 4:
                logger.warning("feishu push queue saturated, drop one message")
                return
            self._inflight += 1
        thread = threading.Thread(
            target=self._deliver_guarded,
            args=(url, payload),
            name="feishu-push",
            daemon=True,
        )
        thread.start()

    def _deliver_guarded(self, url: str, payload: dict[str, Any]) -> None:
        try:
            ok, detail = self._sender(url, payload)
            if not ok:
                logger.warning("feishu push failed: %s (%s)", detail, url[-16:])
        except Exception:
            logger.exception("feishu push crashed")
        finally:
            with self._dispatch_lock:
                self._inflight -= 1

    def send_test(self, webhook_url: str) -> tuple[bool, str]:
        url = validate_feishu_webhook(webhook_url)
        return self._sender(url, build_test_card(url))

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import httpx

from app.config import AppSettings


class AiClientError(RuntimeError):
    pass


class AIAnalysisClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.secrets = settings.secret_config

    @property
    def available(self) -> bool:
        return self._provider_config() is not None

    def provider_label(self) -> str:
        provider = self._provider_config()
        return str(provider.get("name")) if provider else "unavailable"

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        provider = self._provider_config()
        if not provider:
            raise AiClientError("未配置可用的 AI 接口。")

        prompt = self._build_prompt(payload)
        raw_text = self._chat_completion(provider, prompt)
        result = self._parse_result(raw_text)
        return {
            "provider": provider.get("name", "ai"),
            "model": provider.get("model"),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "status": "ok",
            "result": result,
            "raw_text": raw_text,
        }

    def _provider_config(self) -> dict[str, str] | None:
        if self.secrets.get("cf_base_url") and self.secrets.get("cf_key"):
            return {
                "name": "cf_proxy",
                "base_url": str(self.secrets["cf_base_url"]),
                "api_key": str(self.secrets["cf_key"]),
                "model": str(self.secrets.get("cf_model_id") or "gpt-5.5"),
            }
        if self.secrets.get("deepseek-key"):
            return {
                "name": "deepseek",
                "base_url": "https://api.deepseek.com",
                "api_key": str(self.secrets["deepseek-key"]),
                "model": "deepseek-v4-flash",
            }
        if self.secrets.get("zhipu_key"):
            return {
                "name": "zhipu",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": str(self.secrets["zhipu_key"]),
                "model": "glm-4.5",
            }
        if self.secrets.get("bailian_key"):
            return {
                "name": "bailian",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": str(self.secrets["bailian_key"]),
                "model": "qwen-plus",
            }
        if self.secrets.get("huoshan_key"):
            return {
                "name": "huoshan",
                "base_url": "https://ark.cn-beijing.volces.com/api/v3",
                "api_key": str(self.secrets["huoshan_key"]),
                "model": "ep-20240831130000-xxxxx",
            }
        return None

    def _build_prompt(self, payload: dict[str, Any]) -> str:
        context = json.dumps(payload, ensure_ascii=False, indent=2)
        return (
            "你是一个严谨的A股日内做T复盘与盘中辅助分析师，服务对象是有底仓、关注正T/反T和板块共振的交易者。"
            "你必须基于给定的结构化上下文分析，不得编造行情、消息、买卖点或盘口数据。"
            "请只返回严格JSON，不要返回解释性正文，不要使用代码块。"
            "核心口径：盘口逐笔来自 easy_tdx L1 transaction tape，只能称为逐笔成交/成交方向代理；"
            "五档是L1显示档位，不是委托队列或隐藏主力单；没有真实竞价时不能伪造成未匹配委托。"
            "买卖点约束：真正可画在分时图上的点只能引用 canonical_action_points 或 decision_markers；"
            "如果没有确认点，buy_points/sell_points 返回空数组，并说明需要等待什么证据。"
            "你要重点解释：市场/板块是否顺风，核心票和个股谁先动，分时位置是否有盈亏比，"
            "逐笔成交是承接、主动进攻、放量不涨还是抛压，开盘竞价是否提供增量信息，"
            "消息面是否加强或削弱盘面判断，持仓可卖数量和T+1是否影响可执行性。"
            "JSON字段必须包含："
            "summary, decision, t_direction, confidence, confidence_reason, "
            "market_read, sector_read, stock_read, tape_read, opening_read, message_read, position_read, "
            "decision_basis, buy_points, sell_points, risk, invalidation, next_action, watch_items。"
            "其中 decision 只能是 买T / 观察 / 减T/卖T 之一；"
            "t_direction 只能是 正T / 反T / 不做T / 观察；confidence 为0-100整数。"
            "decision_basis、risk、watch_items 都是字符串数组。"
            "buy_points 和 sell_points 都是数组，每个元素包含 time, action, reason, executable, risk。"
            "invalidation 用一句话写清楚什么情况说明判断失效；next_action 用一句话写当前最该盯的动作。"
            "不要把研究中或样本不足的信号包装成确定性策略；当 validation_status 不是 deployable 时，必须降级表述。"
            "根据以下上下文分析：\n"
            f"{context}"
        )

    def _chat_completion(self, provider: dict[str, str], prompt: str) -> str:
        base_url = provider["base_url"].rstrip("/")
        if base_url.endswith("/v1"):
            url = f"{base_url}/chat/completions"
        else:
            url = f"{base_url}/v1/chat/completions"

        headers = {
            "Authorization": f"Bearer {provider['api_key']}",
            "Content-Type": "application/json",
            "api-key": provider["api_key"],
        }
        payload = {
            "model": provider["model"],
            "messages": [
                {"role": "system", "content": "你是一个严谨的A股短线分析助手。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }
        with httpx.Client(timeout=40) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        return self._extract_text(data)

    def _extract_text(self, data: dict[str, Any]) -> str:
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] if isinstance(choices[0], dict) else {}
                message = first.get("message")
                if isinstance(message, dict) and message.get("content"):
                    return str(message["content"])
                if first.get("text"):
                    return str(first["text"])
            if data.get("output_text"):
                return str(data["output_text"])
            if data.get("content"):
                return str(data["content"])
        return json.dumps(data, ensure_ascii=False)

    def _parse_result(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        parsed = self._try_parse_json(cleaned)
        if parsed is not None:
            return parsed
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start != -1 and end > start:
            parsed = self._try_parse_json(cleaned[start : end + 1])
            if parsed is not None:
                return parsed
        return {"summary": cleaned, "decision": "观察", "raw": cleaned}

    def _try_parse_json(self, value: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else {"value": parsed}

"""LLM 服务封装：OpenAI 兼容协议（DeepSeek / Qwen / Gemini 兼容网关 / Ollama）。

依据 docs/architecture.md §2、§10.1：
- 模型服务通过环境变量配置，API Key 不入库、不写死。
- 未配置 API Key 时返回 is_available=False，由调用方降级到
  规则抽取器（对应评测计划 §11 的"本地快速迭代"路径），
  并在工具日志中明确标注降级，保证零编造约束仍然生效。

环境变量：
- OFFICEFLOW_MODEL_BASE_URL：API 地址，默认 DeepSeek 官方
- OFFICEFLOW_MODEL_API_KEY：API Key（必填才启用 LLM）
- OFFICEFLOW_MODEL_NAME：模型名，默认 deepseek-chat
- OFFICEFLOW_MODEL_TIMEOUT：超时秒数，默认 60
"""

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger("officeflow.llm")

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL_NAME = "deepseek-chat"


class LLMClient:
    """最小 OpenAI 兼容 Chat Completions 客户端（仅同步、非流式）。

    支持两种调用模式：
    1. chat_json：单轮 system+user，要求 JSON 输出（用于意图分类、结构化抽取）。
    2. chat_with_tools：多轮消息 + tools 声明，支持 function calling（用于 ReAct 循环）。
    """

    def __init__(self) -> None:
        self.base_url = os.getenv("OFFICEFLOW_MODEL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")
        self.api_key = os.getenv("OFFICEFLOW_MODEL_API_KEY", "")
        self.model_name = os.getenv("OFFICEFLOW_MODEL_NAME", DEFAULT_MODEL_NAME)
        try:
            self.timeout = float(os.getenv("OFFICEFLOW_MODEL_TIMEOUT", "60"))
        except ValueError:
            self.timeout = 60.0

    @property
    def is_available(self) -> bool:
        """是否配置了 API Key（未配置时走规则降级路径）。"""
        return bool(self.api_key)

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
    ) -> Optional[dict[str, Any]]:
        """调用模型并要求 JSON 输出；失败返回 None（由调用方降级/报错）。"""
        if not self.is_available:
            return None
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return self._parse_json(content)
        except Exception as exc:  # noqa: BLE001 - 网络层错误统一降级
            logger.warning("LLM 调用失败，降级处理：%s", exc)
            return None

    def chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0.1,
    ) -> Optional[dict[str, Any]]:
        """带 tools 声明的多轮对话（OpenAI function calling 协议）。

        Args:
            messages: OpenAI 消息列表，可含 assistant 的 tool_calls 与 tool 角色的结果。
            tools: OpenAI tools 声明（JSON Schema 格式）。
            temperature: 采样温度。

        Returns:
            {"content": str|None, "tool_calls": list|None}；失败返回 None。
            - content: 模型文本回复（最终答案或推理过程）。
            - tool_calls: 模型要求调用的工具列表，每项含
              {"id": str, "name": str, "arguments": dict}。
        """
        if not self.is_available:
            return None
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": messages,
            "temperature": temperature,
        }
        # 仅当传入 tools 时才加到 payload（部分端点对空 tools 数组敏感）
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            message = resp.json()["choices"][0]["message"]
            raw_tool_calls = message.get("tool_calls") or []
            parsed_calls: list[dict[str, Any]] = []
            for tc in raw_tool_calls:
                fn = tc.get("function", {})
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    logger.warning("tool_call arguments 不是合法 JSON：%r", args_raw[:200])
                    args = {}
                parsed_calls.append({
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "arguments": args,
                })
            return {
                "content": message.get("content"),
                "tool_calls": parsed_calls,
            }
        except Exception as exc:  # noqa: BLE001 - 网络层错误统一降级
            logger.warning("LLM chat_with_tools 调用失败，降级处理：%s", exc)
            return None

    @staticmethod
    def _parse_json(content: str) -> Optional[dict[str, Any]]:
        """解析模型返回的 JSON（容忍 ```json 代码块包裹）。"""
        content = content.strip()
        fence = re.search(r"```(?:json)?\s*(.+?)```", content, re.DOTALL)
        if fence:
            content = fence.group(1).strip()
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("LLM 返回内容不是合法 JSON：%r", content[:200])
            return None
        return parsed if isinstance(parsed, dict) else None


_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """进程级单例，便于测试 monkeypatch。"""
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm

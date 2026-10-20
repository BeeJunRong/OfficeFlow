"""ReAct 循环节点：Thought → Action → Observation 循环。

依据 docs/architecture.md §5 改造为 ReAct 架构：
- LLM 通过 function calling 自主选择工具与顺序（不再由 route_node 一次性决定）。
- 每轮：LLM 输出 thought（content）+ tool_calls → 执行工具 → 把结果回填为
  tool 角色消息 → 进入下一轮，直到 LLM 不再调用工具（content 即最终答案）。
- 最大迭代次数受限（默认 8），超出时把当前 markdown 直接返回，避免死循环。
- LLM 不可用时降级到原有 route → Skill 流水线（保留零编造与离线可用性）。

状态字段（新增）：
- react_messages：OpenAI 消息列表（多轮对话历史，含 tool_calls 与 tool 结果）。
- react_iteration：当前迭代轮数。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.llm import get_llm
from app.react_tools import TOOL_SPECS, call_tool
from app.schemas import AgentState

logger = logging.getLogger("officeflow.react")

MAX_ITERATIONS = 8

REACT_SYSTEM_PROMPT = """你是 OfficeFlow 办公助手，采用 ReAct（Reasoning + Acting）模式工作。

可用工具已通过 function calling 声明。每一轮你可以：
1. 调用一个工具（function call）来获取信息或执行动作；
2. 根据工具返回的 observation 决定下一步：继续调用其他工具，或给出最终答案。

工作流程示例：
- 会议纪要：extract_fields → validate → render_markdown
- 周报：extract_fields → validate → build_weekly_report → render_weekly
- 邮件草稿：extract_fields → validate → build_email_draft → render_email
- 网页整理：web_fetch → web_summarize → render_web_summary

规则：
- 严格零编造：只使用原文明确出现的信息，缺失字段填"待确认"。
- 工具间有依赖：render_* 必须在对应 build_*/extract_* 之后调用。
- 当所有必要工具调用完毕、Markdown 已生成时，用一句话告知用户任务完成，不再调用工具。
- 不要重复调用同一工具传递相同参数。
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log(tool_name: str, status: str, error_message: str = "") -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": status,
        "error_message": error_message,
        "timestamp": _now(),
    }


def react_node(state: AgentState) -> AgentState:
    """ReAct 循环节点：LLM 自主选择工具直到给出最终答案或达到迭代上限。"""
    llm = get_llm()
    if not llm.is_available:
        # 降级路径由 graph 层处理（返回原 state 让条件边走 fallback）
        return {**state, "_react_fallback": True}

    logs: list[dict[str, Any]] = list(state.get("tool_logs", []))
    user_text = state.get("input_text", "").strip()
    if not user_text:
        logs.append(_log("react", "failed", "输入为空"))
        return {**state, "tool_logs": logs, "error_message": "请输入非空内容"}

    # 初始化消息历史
    messages: list[dict[str, Any]] = list(state.get("react_messages", []))
    if not messages:
        messages = [
            {"role": "system", "content": REACT_SYSTEM_PROMPT},
            {"role": "user", "content": user_text},
        ]

    # 累积 state_updates（每轮工具返回的 state_updates 合并进 state）
    accumulated: dict[str, Any] = {}
    # 临时上下文（如 _web_page 跨工具传递，不进 AgentState）
    tool_context: dict[str, Any] = dict(state.get("_tool_context", {}))

    iteration = int(state.get("react_iteration", 0))
    logs.append(_log("react", "success", f"开始 ReAct 循环，输入 {len(user_text)} 字"))

    while iteration < MAX_ITERATIONS:
        iteration += 1
        response = llm.chat_with_tools(messages, TOOL_SPECS)
        if response is None:
            logs.append(_log("react", "failed", f"第 {iteration} 轮 LLM 调用失败"))
            break

        content = response.get("content")
        tool_calls = response.get("tool_calls") or []

        # 把 assistant 消息追加到历史（含 tool_calls 供下一轮参考）
        assistant_msg: dict[str, Any] = {"role": "assistant"}
        if content:
            assistant_msg["content"] = content
        if tool_calls:
            # 还原 OpenAI tool_calls 原始结构
            assistant_msg["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": _dumps_args(tc["arguments"]),
                    },
                }
                for tc in tool_calls
            ]
        messages.append(assistant_msg)

        if not tool_calls:
            # LLM 不再调用工具 → 最终答案
            logs.append(_log("react", "success", f"第 {iteration} 轮给出最终答案"))
            break

        # 执行每个工具调用，把结果回填为 tool 角色消息
        for tc in tool_calls:
            name = tc["name"]
            args = tc["arguments"] or {}
            # 注入跨工具上下文（如 web_fetch 的结果给 web_summarize 用）
            args = _inject_context(args, name, tool_context, state, accumulated)

            logs.append(_log(name, "running"))
            result = call_tool(name, args)
            summary = result.get("summary", "")
            state_updates = result.get("state_updates", {})

            # 分离临时上下文与持久 state
            for k, v in state_updates.items():
                if k.startswith("_"):
                    tool_context[k] = v
                else:
                    accumulated[k] = v

            # 回填 tool 角色消息（OpenAI 协议要求 tool_call_id）
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": summary,
            })
            logs.append(_log(name, "success", summary[:200]))

    if iteration >= MAX_ITERATIONS:
        logs.append(_log("react", "failed", f"达到最大迭代 {MAX_ITERATIONS}，强制结束"))

    # 合并最终 state
    new_state: AgentState = {
        **state,
        **accumulated,
        "tool_logs": logs,
        "react_messages": messages,
        "react_iteration": iteration,
        "_tool_context": tool_context,
        "error_message": accumulated.get("error_message", state.get("error_message", "")),
    }
    # 确保 task_type 有值（供前端展示与持久化；ReAct 不依赖它但 API 层可能用）
    if not new_state.get("task_type"):
        new_state["task_type"] = _infer_task_type(accumulated)
    new_state.pop("_react_fallback", None)
    return new_state


def _dumps_args(args: Any) -> str:
    import json
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False)


def _inject_context(
    args: dict[str, Any],
    tool_name: str,
    tool_context: dict[str, Any],
    state: AgentState,
    accumulated: dict[str, Any],
) -> dict[str, Any]:
    """为工具注入跨工具上下文。

    例如 web_summarize 需要 web_fetch 的 page；render_* 需要 build_* 的结果。
    若 LLM 已在 arguments 中显式给出，则不覆盖。
    """
    args = dict(args)

    if tool_name == "web_summarize":
        page = tool_context.get("_web_page")
        if page:
            args.setdefault("title", page.get("title", ""))
            args.setdefault("text", page.get("text", ""))
            args.setdefault("url", page.get("url", ""))

    if tool_name == "render_markdown":
        minutes = accumulated.get("structured_result") or state.get("structured_result")
        if minutes and "minutes" not in args:
            args["minutes"] = minutes

    if tool_name == "build_weekly_report":
        minutes = accumulated.get("structured_result") or state.get("structured_result")
        if minutes and "minutes" not in args:
            args["minutes"] = minutes

    if tool_name == "render_weekly":
        report = accumulated.get("weekly_report") or state.get("weekly_report")
        if report and "weekly_report" not in args:
            args["weekly_report"] = report

    if tool_name == "build_email_draft":
        if "minutes" not in args and "weekly_report" not in args:
            minutes = accumulated.get("structured_result") or state.get("structured_result")
            report = accumulated.get("weekly_report") or state.get("weekly_report")
            if report:
                args["weekly_report"] = report
            elif minutes:
                args["minutes"] = minutes

    if tool_name == "render_email":
        draft = accumulated.get("email_draft") or state.get("email_draft")
        if draft and "email_draft" not in args:
            args["email_draft"] = draft

    if tool_name == "validate":
        if "minutes" not in args:
            minutes = accumulated.get("structured_result") or state.get("structured_result")
            if minutes:
                args["minutes"] = minutes
        if "source_text" not in args:
            args["source_text"] = state.get("input_text", "")

    return args


def _infer_task_type(accumulated: dict[str, Any]) -> str:
    """根据最终 state 推断 task_type（供前端展示与持久化）。"""
    if accumulated.get("email_draft"):
        return "email_draft"
    if accumulated.get("weekly_report"):
        return "weekly_report"
    if accumulated.get("structured_result"):
        result = accumulated["structured_result"]
        # WebSummary 有 url/points 字段，MeetingMinutes 有 decisions/action_items
        if "points" in result and "url" in result:
            return "web_summary"
        return "meeting_minutes"
    return ""

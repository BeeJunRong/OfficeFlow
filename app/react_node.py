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
- reasoning_trace：结构化决策追溯列表，每项含
  {step, thought, action, action_input, observation, status}，
  供前端像豆包/DeepSeek 平台那样展示推理过程。
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

多轮对话与多步规划：
- 用户可能在一轮里要求多个任务（如"做会议纪要和周报，然后发邮件"），你需要依次完成每个任务。
- 用户可能引用上一轮的结果（如"继续生成周报"），此时应复用上一轮的产物而非重新抽取。
  若历史上下文显示已生成会议纪要，且用户要求"继续生成周报"，直接调用
  build_weekly_report（用已有的 structured_result）→ render_weekly，跳过 extract_fields。
- 每完成一个子任务，用一句话告知进度（如"会议纪要已生成，接下来整理周报"），再继续下一步。

规则：
- 严格零编造：只使用原文明确出现的信息，缺失字段填"待确认"。
- 工具间有依赖：render_* 必须在对应 build_*/extract_* 之后调用。
- 当所有必要工具调用完毕、Markdown 已生成时，用一句话告知用户任务完成，不再调用工具。
- 不要重复调用同一工具传递相同参数。
"""


def _build_history_context(history: list[dict[str, Any]] | None) -> str:
    """把历史对话压缩为 system prompt 的一段上下文摘要。

    让 LLM 知道之前生成了什么，支持"继续生成周报"等多轮派生。
    """
    if not history:
        return ""
    lines = ["\n\n=== 历史对话上下文 ==="]
    for i, turn in enumerate(history, 1):
        role = turn.get("role", "")
        content = turn.get("content", "")
        task_type = turn.get("task_type", "")
        has_result = turn.get("has_result", False)
        if role == "user":
            lines.append(f"[轮{i} 用户] {content[:200]}")
        elif role == "assistant":
            task_label = f"（任务：{task_type}）" if task_type else ""
            result_label = "已生成产物" if has_result else "无产物"
            lines.append(f"[轮{i} 助手{task_label}] {result_label}：{content[:200]}")
    lines.append("=== 历史对话上下文结束 ===\n")
    return "\n".join(lines)


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
        # 注入历史对话上下文到 system prompt，让 LLM 感知之前生成了什么
        history_ctx = _build_history_context(state.get("history"))
        # 跨轮"接着做"：上一轮产物已由 API 层注入状态，提示模型直接复用
        seed_hint = ""
        if any(
            state.get(key)
            for key in ("structured_result", "weekly_report", "email_draft")
        ):
            seed_hint = (
                "\n\n【系统状态】已从上一轮对话恢复结构化产物（会议纪要/周报/邮件草稿）。"
                "若用户要求派生（整理周报、生成/发送邮件），直接调用 "
                "build_weekly_report / build_email_draft / render_*，"
                "不要重新调用 extract_fields 覆盖已有结果。"
            )
        messages = [
            {
                "role": "system",
                "content": REACT_SYSTEM_PROMPT + history_ctx + seed_hint,
            },
            {"role": "user", "content": user_text},
        ]

    # 累积 state_updates（每轮工具返回的 state_updates 合并进 state）
    accumulated: dict[str, Any] = {}
    # 多步产物列表：每完成一个子任务（render_markdown/render_weekly/render_email）
    # 追加一项 {task_type, markdown, structured_result/weekly_report/email_draft}，
    # 避免后续 markdown 覆盖前一步产物。
    outputs: list[dict[str, Any]] = list(state.get("outputs", []))
    # 临时上下文（如 _web_page 跨工具传递，不进 AgentState）
    tool_context: dict[str, Any] = dict(state.get("_tool_context", {}))

    iteration = int(state.get("react_iteration", 0))
    logs.append(_log("react", "success", f"开始 ReAct 循环，输入 {len(user_text)} 字"))

    # 决策追溯：每轮的 thought / action / observation
    reasoning_trace: list[dict[str, Any]] = list(state.get("reasoning_trace", []))

    while iteration < MAX_ITERATIONS:
        iteration += 1
        response = llm.chat_with_tools(messages, TOOL_SPECS)
        if response is None:
            logs.append(_log("react", "failed", f"第 {iteration} 轮 LLM 调用失败"))
            reasoning_trace.append({
                "step": iteration,
                "thought": "",
                "action": "",
                "action_input": {},
                "observation": "LLM 调用失败",
                "status": "failed",
            })
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
            reasoning_trace.append({
                "step": iteration,
                "thought": content or "",
                "action": "",
                "action_input": {},
                "observation": "",
                "status": "final",
            })
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

            # 多步产物收集：render_* 工具产生 markdown 时追加到 outputs
            # 避免!后续 markdown 覆盖前一步产物（如周报覆盖会议纪要）
            if name in ("render_markdown", "render_weekly", "render_email", "render_web_summary") and state_updates.get("markdown"):
                output_entry: dict[str, Any] = {"markdown": state_updates["markdown"]}
                if name == "render_markdown" and accumulated.get("structured_result"):
                    output_entry["task_type"] = "meeting_minutes"
                    output_entry["structured_result"] = accumulated["structured_result"]
                elif name == "render_weekly" and accumulated.get("weekly_report"):
                    output_entry["task_type"] = "weekly_report"
                    output_entry["weekly_report"] = accumulated["weekly_report"]
                elif name == "render_email" and accumulated.get("email_draft"):
                    output_entry["task_type"] = "email_draft"
                    output_entry["email_draft"] = accumulated["email_draft"]
                elif name == "render_web_summary" and accumulated.get("structured_result"):
                    output_entry["task_type"] = "web_summary"
                    output_entry["structured_result"] = accumulated["structured_result"]
                else:
                    output_entry["task_type"] = name.replace("render_", "")
                outputs.append(output_entry)

            # 回填 tool 角色消息（OpenAI 协议要求 tool_call_id）
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": summary,
            })
            logs.append(_log(name, "success", summary[:200]))
            reasoning_trace.append({
                "step": iteration,
                "thought": content or "",
                "action": name,
                "action_input": args,
                "observation": summary,
                "status": "success",
            })

    if iteration >= MAX_ITERATIONS:
        logs.append(_log("react", "failed", f"达到最大迭代 {MAX_ITERATIONS}，强制结束"))

    # 兜底渲染：LLM 提前结束、漏调 render_* 或迭代耗尽时，
    # 仍按已产出的结构化结果确定性地生成 Markdown，保证用户拿到最终结果。
    outputs = _ensure_outputs(accumulated, outputs, logs)
    markdown = accumulated.get("markdown") or (outputs[-1]["markdown"] if outputs else "")

    # 合并最终 state
    new_state: AgentState = {
        **state,
        **accumulated,
        "markdown": markdown,
        "routed_by": state.get("routed_by") or "react",
        "tool_logs": logs,
        "react_messages": messages,
        "react_iteration": iteration,
        "reasoning_trace": reasoning_trace,
        "outputs": outputs,
        "_tool_context": tool_context,
        "error_message": accumulated.get("error_message", state.get("error_message", "")),
    }
    # 确保 task_type 有值（多步规划时取第一个产物的类型作为主任务类型）
    if not new_state.get("task_type"):
        new_state["task_type"] = (
            outputs[0].get("task_type", "") if outputs else _infer_task_type(accumulated)
        )
    new_state.pop("_react_fallback", None)
    return new_state


def _ensure_outputs(
    accumulated: dict[str, Any],
    outputs: list[dict[str, Any]],
    logs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """兜底渲染：为每个已产出的结构化结果补齐 Markdown 产物。

    ReAct 的最终结果依赖 LLM 主动调用 render_*；当模型提前结束、达到迭代上限
    或漏调渲染工具时，这里按产物类型确定性地补齐，保证前端始终有最终结果。
    """
    seen = {o.get("task_type") for o in outputs}

    def _add(
        tool_name: str,
        task_type: str,
        args: dict[str, Any],
        product_key: str,
        product: Any,
    ) -> None:
        if task_type in seen:
            return
        result = call_tool(tool_name, args)
        markdown = (result.get("state_updates") or {}).get("markdown", "")
        if not markdown:
            return
        outputs.append(
            {"task_type": task_type, "markdown": markdown, product_key: product}
        )
        seen.add(task_type)
        logs.append(_log(tool_name, "success", "兜底自动渲染（LLM 未调用渲染工具）"))

    structured = accumulated.get("structured_result")
    weekly = accumulated.get("weekly_report")
    email = accumulated.get("email_draft")

    if _looks_like_web(structured):
        _add(
            "render_web_summary",
            "web_summary",
            {
                "title": structured.get("title", ""),
                "url": structured.get("url", ""),
                "points": structured.get("points", []),
                "word_count": structured.get("word_count", 0),
            },
            "structured_result",
            structured,
        )
    elif _looks_like_minutes(structured):
        _add(
            "render_markdown",
            "meeting_minutes",
            {"minutes": structured},
            "structured_result",
            structured,
        )

    if weekly:
        _add(
            "render_weekly",
            "weekly_report",
            {"weekly_report": weekly},
            "weekly_report",
            weekly,
        )
    if email:
        _add(
            "render_email",
            "email_draft",
            {"email_draft": email},
            "email_draft",
            email,
        )
    return outputs


def _dumps_args(args: Any) -> str:
    import json
    if isinstance(args, str):
        return args
    return json.dumps(args, ensure_ascii=False)


def _looks_like_minutes(data: Any) -> bool:
    """判断结构化结果是否是会议纪要（而非 WebSummary）。"""
    return isinstance(data, dict) and (
        "action_items" in data or "decisions" in data or "topic" in data
    )


def _looks_like_web(data: Any) -> bool:
    """判断结构化结果是否是网页整理产物。"""
    return isinstance(data, dict) and "points" in data and "url" in data


def _inject_context(
    args: dict[str, Any],
    tool_name: str,
    tool_context: dict[str, Any],
    state: AgentState,
    accumulated: dict[str, Any],
) -> dict[str, Any]:
    """为工具注入跨工具上下文：**数据一律以状态机中的真实产物为准**。

    关键设计（修复"validate 反序列化失败"循环）：
    LLM 在 function calling 的 arguments 里转述大块数据时，会丢字段、改键名
    （如把 ActionItem.task 写成 content）、甚至截断。因此 validate / render_* /
    build_* 所需的数据**始终从 accumulated/state 注入并覆盖**，LLM 只负责决定
    "调用哪个工具"，不负责搬运数据。

    例外：工具自身产出的一次性上下文（如 web_fetch 的页面）从 tool_context 注入。
    """
    args = dict(args)
    minutes = accumulated.get("structured_result") or state.get("structured_result")
    weekly = accumulated.get("weekly_report") or state.get("weekly_report")
    email = accumulated.get("email_draft") or state.get("email_draft")

    # 清理后文本用于零编造核对；原始文本用于计算"原文第几行"
    raw_text = state.get("raw_input_text") or state.get("input_text", "")

    if tool_name == "validate":
        if _looks_like_minutes(minutes):
            args["minutes"] = minutes
        # 原文始终用本轮清理后的输入，防止 LLM 转述失真
        args["source_text"] = state.get("input_text", "")
        args["line_source_text"] = raw_text

    elif tool_name == "render_markdown":
        if _looks_like_minutes(minutes):
            args["minutes"] = minutes

    elif tool_name == "build_weekly_report":
        if _looks_like_minutes(minutes):
            args["minutes"] = minutes
        args["source_text"] = raw_text

    elif tool_name == "render_weekly":
        if weekly:
            args["weekly_report"] = weekly

    elif tool_name == "build_email_draft":
        # 有周报优先用周报（含更完整的计划信息），否则用纪要
        if weekly:
            args["weekly_report"] = weekly
            args.pop("minutes", None)
        elif _looks_like_minutes(minutes):
            args["minutes"] = minutes

    elif tool_name == "render_email":
        if email:
            args["email_draft"] = email

    elif tool_name == "web_summarize":
        page = tool_context.get("_web_page")
        if page:
            args["title"] = page.get("title", "")
            args["text"] = page.get("text", "")
            args["url"] = page.get("url", "")

    elif tool_name == "render_web_summary":
        if _looks_like_web(minutes):
            for key in ("title", "url", "points", "word_count"):
                if key in minutes:
                    args[key] = minutes[key]

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

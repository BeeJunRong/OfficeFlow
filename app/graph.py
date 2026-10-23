"""LangGraph 工作流（architecture.md §5，双通道架构）。

双通道：
- 通道 1（显式点击 → Skill 硬编码流水线）：task_type 非空时直接走 Skill，
  确定性、零编造、可验收。不经过 ReAct、不经过路由（task_type 已知）。
- 通道 2（"自动识别" → ReAct）：task_type 为空时走 ReAct 循环，
  LLM 通过 function calling 自主选择工具。
  LLM 不可用时降级到 route → Skill（保留离线可用性）。

状态图：
  START → dispatch → (skill_execute | react)
    skill_execute → END
    react → (fallback_route?) → (fallback_execute?) → END

导出/重试不走图，由 API 层显式触发（Q-02：导出必须用户点击）。
"""

from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

from app.cleaning import clean_input_text
from app.react_node import react_node
from app.router import route_task, strip_template_prefix
from app.schemas import AgentState
from app.skills import get_skill


def _log(tool_name: str, status: str, error_message: str = "") -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": status,
        "error_message": error_message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def dispatch_node(state: AgentState) -> AgentState:
    """分发节点：清理输入，记录通道选择。

    本节点不做执行，只做输入清理与通道标记，由条件边决定走哪条通道。
    保留原始输入（raw_input_text）供降级路由使用——strip_template_prefix
    会剥掉"请帮我整理会议纪要："前缀，但降级路由需要该前缀做关键词识别。
    """
    raw = state.get("input_text", "")
    body = strip_template_prefix(raw)
    cleaned = clean_input_text(body)
    return {**state, "input_text": cleaned, "raw_input_text": raw}


def skill_execute_node(state: AgentState) -> AgentState:
    """通道 1：显式点击 → Skill 硬编码流水线。

    task_type 已由前端确定，直接分发到对应 Skill，不经过 ReAct、不经过路由。
    保留原有零编造、可验收、离线可用特性。
    """
    task_type = state.get("task_type", "")
    logs: list[dict[str, Any]] = list(state.get("tool_logs", []))
    logs.append(_log("route", "success", f"显式选择 task_type={task_type}（explicit）"))
    skill = get_skill(task_type)
    result = skill.run({**state, "routed_by": "explicit", "tool_logs": logs})
    return result


def react_entry_node(state: AgentState) -> AgentState:
    """通道 2 入口：自动识别 → ReAct 循环。"""
    return react_node(state)


def fallback_route_node(state: AgentState) -> AgentState:
    """降级路由：LLM 不可用时用分层路由（关键词→LLM 兜底）确定 task_type。

    用原始输入（raw_input_text）做路由，因为 strip_template_prefix 会剥掉
    "请帮我整理会议纪要："前缀，而降级路由需要该前缀做关键词识别。
    """
    route_text = state.get("raw_input_text") or state.get("input_text", "")
    result = route_task(route_text, state.get("task_type"))
    if result.needs_clarification or not result.task_type:
        return {
            **state,
            "task_type": "",
            "routed_by": result.routed_by,
            "needs_clarification": True,
            "error_message": "无法确定任务类型，请点击下方的任务按钮选择（会议纪要/周报整理/邮件草稿/网页整理）",
            "tool_logs": [
                *state.get("tool_logs", []),
                _log("route", "failed", "LLM 不可用且无法确定任务类型"),
            ],
        }
    return {
        **state,
        "task_type": result.task_type,
        "routed_by": result.routed_by,
        "needs_clarification": False,
        "tool_logs": [
            *state.get("tool_logs", []),
            _log("route", "success", f"降级路由 task_type={result.task_type}（{result.routed_by}）"),
        ],
    }


def fallback_execute_node(state: AgentState) -> AgentState:
    """降级执行：分发到 Skill（保留原有零编造流水线）。"""
    if state.get("needs_clarification"):
        return state
    skill = get_skill(state["task_type"])
    return skill.run(state)


def should_dispatch(state: AgentState) -> str:
    """条件边：task_type 非空走 Skill 通道，否则走 ReAct 通道。"""
    if state.get("task_type"):
        return "skill_execute"
    return "react"


def should_fallback(state: AgentState) -> str:
    """条件边：ReAct 标记降级时走 fallback 路径。"""
    if state.get("_react_fallback"):
        return "fallback_route"
    return "end"


def should_execute(state: AgentState) -> str:
    """降级路径条件边：需要追问时直接结束。"""
    return "end" if state.get("needs_clarification") else "execute"


def build_graph() -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("dispatch", dispatch_node)
    graph.add_node("skill_execute", skill_execute_node)
    graph.add_node("react", react_entry_node)
    graph.add_node("fallback_route", fallback_route_node)
    graph.add_node("fallback_execute", fallback_execute_node)
    graph.add_edge(START, "dispatch")
    graph.add_conditional_edges(
        "dispatch", should_dispatch, {"skill_execute": "skill_execute", "react": "react"}
    )
    graph.add_edge("skill_execute", END)
    graph.add_conditional_edges(
        "react", should_fallback, {"fallback_route": "fallback_route", "end": END}
    )
    graph.add_conditional_edges(
        "fallback_route", should_execute, {"execute": "fallback_execute", "end": END}
    )
    graph.add_edge("fallback_execute", END)
    return graph.compile()


# 进程级编译缓存
_compiled = None


def run_workflow(state: AgentState) -> AgentState:
    """运行一次完整工作流（生成纪要，不含导出）。"""
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    result: dict[str, Any] = _compiled.invoke(state)
    return result  # type: ignore[no-any-return]

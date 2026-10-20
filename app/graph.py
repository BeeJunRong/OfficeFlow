"""LangGraph 工作流（architecture.md §5，ReAct 架构）。

状态图：react → (fallback?) → END
- ReAct 节点：LLM 通过 function calling 自主选择工具与顺序（Thought-Action-Observation 循环）。
- 降级路径：LLM 不可用时走 route → Skill 流水线（保留零编造与离线可用性）。
- 导出/重试不走图，由 API 层显式触发（Q-02：导出必须用户点击）。
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


def react_entry_node(state: AgentState) -> AgentState:
    """ReAct 入口：清理输入后进入 ReAct 循环。"""
    body = strip_template_prefix(state.get("input_text", ""))
    cleaned = clean_input_text(body)
    return react_node({**state, "input_text": cleaned})


def fallback_route_node(state: AgentState) -> AgentState:
    """降级路由：LLM 不可用时用分层路由（显式→关键词）确定 task_type。"""
    result = route_task(state.get("input_text", ""), state.get("task_type"))
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
    graph.add_node("react", react_entry_node)
    graph.add_node("fallback_route", fallback_route_node)
    graph.add_node("fallback_execute", fallback_execute_node)
    graph.add_edge(START, "react")
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

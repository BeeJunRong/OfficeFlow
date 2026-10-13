"""LangGraph 工作流（architecture.md §5）。

状态图：route → skill_execute → (export 由用户显式触发，独立于图)
- 路由节点：分层路由确定 task_type（显式 → 指令区关键词 → LLM 兜底）
- 执行节点：分发到对应 Skill；低置信度时直接返回追问
导出/重试不走图，由 API 层显式触发（Q-02：导出必须用户点击）。
"""

from datetime import datetime, timezone
from typing import Any

from langgraph.graph import END, START, StateGraph

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


def route_node(state: AgentState) -> AgentState:
    """路由节点：FR-00 分层路由（路由步骤记入工具日志，供前端展示过程）。"""
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
                _log("route", "failed", "无法确定任务类型（低置信度或无可用分类器）"),
            ],
        }
    return {
        **state,
        "task_type": result.task_type,
        "routed_by": result.routed_by,
        "needs_clarification": False,
        "tool_logs": [
            *state.get("tool_logs", []),
            _log("route", "success", f"task_type={result.task_type}（{result.routed_by}）"),
        ],
    }


def execute_node(state: AgentState) -> AgentState:
    """执行节点：分发到 Skill；会议纪要走完整抽取流程。"""
    if state.get("needs_clarification"):
        return state
    skill = get_skill(state["task_type"])
    # 前端模板填充的指令前缀不参与抽取，只送正文
    body = strip_template_prefix(state.get("input_text", ""))
    return skill.run({**state, "input_text": body})


def should_execute(state: AgentState) -> str:
    """条件边：需要追问时直接结束。"""
    return "end" if state.get("needs_clarification") else "execute"


def build_graph() -> Any:
    graph = StateGraph(AgentState)
    graph.add_node("route", route_node)
    graph.add_node("execute", execute_node)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route", should_execute, {"execute": "execute", "end": END}
    )
    graph.add_edge("execute", END)
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

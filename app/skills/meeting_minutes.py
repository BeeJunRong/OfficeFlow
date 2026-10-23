"""会议纪要 Skill（FR-01，唯一完整实现）。

流程对齐 architecture.md §5：
  解析节点 → 抽取节点 → 校验节点（零编造）→ 缺失值填充 → 生成 Markdown
导出节点由用户显式点击触发，不在本 Skill 内执行（Q-02）。
"""

from datetime import datetime, timezone
from typing import Any

from app.schemas import AgentState, MeetingMinutes, PENDING
from app.skills.base import Skill
from app.tools.export_markdown import render_markdown
from app.tools.extract_fields import extract_meeting_minutes
from app.validate import validate_minutes

JSON_SCHEMA_ACTION_ITEM = {
    "type": "object",
    "properties": {
        "task": {"type": "string"},
        "owner": {"type": "string"},
        "deadline": {"type": "string"},
        "evidence": {"type": "string"},
    },
    "required": ["task"],
}


class MeetingMinutesSkill(Skill):
    name = "meeting_minutes"
    display_name = "会议纪要"
    input_schema = {"type": "string", "description": "会议原始文字记录（建议 ≤4,000 汉字）"}
    output_schema = {
        "type": "object",
        "properties": {
            "date": {"type": "string"},
            "topic": {"type": "string"},
            "decisions": {"type": "array", "items": {"type": "string"}},
            "action_items": {"type": "array", "items": JSON_SCHEMA_ACTION_ITEM},
        },
    }
    tools = ["extract_fields", "validate", "render_markdown"]
    acceptance_samples = "tests/samples/（13 例合成样本，见评测计划 §4）"

    def run(self, state: AgentState) -> AgentState:
        logs: list[dict[str, Any]] = list(state.get("tool_logs", []))
        now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

        def log(tool: str, status: str, error: str = "") -> None:
            logs.append(
                {
                    "tool_name": tool,
                    "status": status,
                    "error_message": error,
                    "timestamp": now(),
                }
            )

        text = state.get("input_text", "").strip()
        if not text:
            log("extract_fields", "failed", "输入为空，已阻止生成")
            return {
                **state,
                "tool_logs": logs,
                "error_message": "请输入非空会议文字记录后再生成纪要",
            }

        # 解析 + 抽取节点
        try:
            minutes, engine = extract_meeting_minutes(text)
            engine_label = {
                "langextract": "引擎=LangExtract 原文溯源",
                "llm": "引擎=LLM",
                "rule": "引擎=规则降级",
            }.get(engine, f"引擎={engine}")
            log("extract_fields", "success", engine_label)
        except Exception as exc:  # noqa: BLE001
            log("extract_fields", "failed", str(exc))
            return {
                **state,
                "tool_logs": logs,
                "error_message": f"抽取工具失败：{exc}，内容已保留，可重试",
            }

        # 校验节点：零编造检查（行号以原始输入为准）
        minutes, corrections = validate_minutes(
            minutes, text, line_source_text=state.get("raw_input_text")
        )
        log(
            "validate",
            "success",
            f"回退{len(corrections)}处" if corrections else "",
        )
        for c in corrections:
            logs.append(
                {
                    "tool_name": "validate",
                    "status": "success",
                    "error_message": c,
                    "timestamp": now(),
                }
            )

        # 缺失值填充（Pydantic 默认值即"待确认"，此处显式兜底空字符串）
        minutes = minutes.model_copy(
            update={
                "date": minutes.date or PENDING,
                "topic": minutes.topic or PENDING,
            }
        )

        # 生成 Markdown
        markdown = render_markdown(minutes)
        log("render_markdown", "success")

        return {
            **state,
            "structured_result": minutes.model_dump(),
            "markdown": markdown,
            "tool_logs": logs,
            "export_status": "pending",
            "error_message": "",
        }

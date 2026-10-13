"""P1 占位 Skill：周报整理 / 邮件草稿 / 网页整理。

仅提供提示按钮与模板填充，生成结果返回"P1 开发中"（proposal.md §4）。
不做真实邮箱集成、批量爬取。
"""

from app.schemas import AgentState
from app.skills.base import Skill


class PlaceholderSkill(Skill):
    input_schema = {"type": "string", "description": "原始文本"}
    output_schema = {"type": "string", "description": "P1 占位提示文本"}
    tools: list[str] = []
    acceptance_samples = "不纳入首版验收"

    def __init__(self, display_name: str) -> None:
        self.display_name = display_name

    @property
    def name(self) -> str:  # type: ignore[override]
        return {
            "周报整理": "weekly_report",
            "邮件草稿": "email_draft",
            "网页整理": "web_summary",
        }[self.display_name]

    def run(self, state: AgentState) -> AgentState:
        return {
            **state,
            "markdown": f"{self.display_name}功能为 P1 开发中，首版仅支持会议纪要（FR-01）。",
            "export_status": "pending",
            "error_message": "",
        }

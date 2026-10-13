"""Pydantic 数据模型：FR-01 会议纪要结构化输出与 Agent 状态。

依据 docs/architecture.md §5.2、§7：
- 所有缺失字段统一默认 "待确认"，不得编造。
- 每条待办附原文证据片段（evidence）。
"""

from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field

PENDING = "待确认"

VALID_TASK_TYPES = ("meeting_minutes", "weekly_report", "email_draft", "web_summary")


class WebSummary(BaseModel):
    """网页整理结构化输出（FR-04；单页读取，不批量爬取、不登录）。"""

    title: str = Field(default=PENDING, description="网页标题")
    url: str = Field(default="", description="来源 URL")
    points: list[str] = Field(default_factory=list, description="内容要点（摘自原文段落）")
    word_count: int = Field(default=0, description="正文约字数")


class ActionItem(BaseModel):
    """单条待办事项。"""

    task: str = Field(description="待办事项")
    owner: str = Field(default=PENDING, description="负责人")
    co_owner: str = Field(default=PENDING, description="协办")
    deadline: str = Field(default=PENDING, description="截止时间")
    deliverable: str = Field(default=PENDING, description="输出物")
    evidence: str = Field(default="", description="原文证据片段")


class NextMeeting(BaseModel):
    """下次会议安排。"""

    time: str = Field(default=PENDING, description="下次会议时间")
    location: str = Field(default=PENDING, description="下次会议地点")
    agenda: str = Field(default=PENDING, description="下次会议议题")


class MeetingMinutes(BaseModel):
    """会议纪要结构化输出（标准六段式模板）。"""

    date: str = Field(default=PENDING, description="会议时间")
    location: str = Field(default=PENDING, description="地点")
    host: str = Field(default=PENDING, description="主持人")
    attendees: list[str] = Field(default_factory=list, description="出席人员")
    observers: list[str] = Field(default_factory=list, description="列席人员")
    absentees: list[str] = Field(default_factory=list, description="缺席人员")
    recorder: str = Field(default=PENDING, description="记录人")
    reviewer: str = Field(default=PENDING, description="审核人")
    topic: str = Field(default=PENDING, description="会议主题")
    agenda: list[str] = Field(default_factory=list, description="会议议题")
    discussion: list[str] = Field(default_factory=list, description="会议内容与讨论")
    decisions: list[str] = Field(default_factory=list, description="会议决议")
    action_items: list[ActionItem] = Field(default_factory=list, description="待办事项")
    risks: list[str] = Field(default_factory=list, description="风险与遗留问题")
    next_meeting: NextMeeting = Field(default_factory=NextMeeting, description="下次会议安排")
    attachments: list[str] = Field(default_factory=list, description="附件")


class WorkItem(BaseModel):
    """周报中的单项工作事项（本周完成 / 下周计划通用）。"""

    content: str = Field(description="工作事项")
    status: str = Field(default=PENDING, description="状态：已完成/进行中/待确认")
    evidence: str = Field(default="", description="原文证据片段")


class WeeklyReport(BaseModel):
    """周报结构化输出（FR-02）。"""

    period: str = Field(default=PENDING, description="统计周期（如 2026-10-06 ~ 2026-10-12）")
    work_done: list[WorkItem] = Field(default_factory=list, description="本周完成工作")
    plans: list[WorkItem] = Field(default_factory=list, description="下周计划")
    risks: list[str] = Field(default_factory=list, description="风险与问题")


class EmailDraft(BaseModel):
    """邮件草稿结构化输出（FR-03；只生成草稿，不发送）。"""

    subject: str = Field(default=PENDING, description="邮件主题")
    recipient: str = Field(default=PENDING, description="收件人称呼")
    sender: str = Field(default=PENDING, description="落款署名")
    key_points: list[str] = Field(default_factory=list, description="正文要点（来自用户输入，不编造）")


class GenerateRequest(BaseModel):
    """POST /api/generate 请求体。"""

    input_text: str = Field(description="用户输入的原始文本")
    task_type: Optional[str] = Field(
        default=None,
        description="显式任务类型（点击提示按钮时由前端传入）；"
        "传入后锁定，不再依据正文关键词路由",
    )


class ToolLog(BaseModel):
    """单次工具调用日志，用于前端工具状态面板与验收证据。"""

    tool_name: str
    status: str  # success / failed
    error_message: str = ""
    timestamp: str  # ISO 8601


class GenerateResponse(BaseModel):
    """POST /api/generate 响应体。"""

    session_id: str
    task_type: str
    routed_by: str  # explicit / keyword / llm / fallback_rule
    needs_clarification: bool = Field(
        default=False, description="路由置信度不足时为 True，前端应提示用户选择任务类型"
    )
    message: str = ""
    structured_result: Optional[dict] = None
    markdown: str = ""
    tool_logs: list[ToolLog] = Field(default_factory=list)
    error_message: str = ""


class ExportRequest(BaseModel):
    """POST /api/export 请求体。"""

    session_id: str
    content: str = Field(description="用户编辑后的 Markdown 内容")
    filename: str = Field(default="meeting_minutes", description="导出文件名（不含扩展名）")
    inject_failure: bool = Field(
        default=False, description="测试环境注入导出失败（E1/E2 场景专用）"
    )


class ExportResponse(BaseModel):
    """POST /api/export 响应体。"""

    success: bool
    file_path: str = ""
    error_message: str = ""


class RetryRequest(BaseModel):
    """POST /api/retry 请求体。重试上一次失败的导出。"""

    session_id: str
    content: str
    filename: str = "meeting_minutes"
    inject_failure: bool = False


class AgentState(TypedDict, total=False):
    """LangGraph 工作流状态（architecture.md §5.2）。"""

    session_id: str
    task_type: str
    routed_by: str
    needs_clarification: bool
    input_text: str
    structured_result: dict[str, Any]
    markdown: str
    edited_content: str
    tool_logs: list[dict[str, Any]]
    export_status: str  # pending / success / failed
    error_message: str

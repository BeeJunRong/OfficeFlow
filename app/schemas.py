"""Pydantic 数据模型：FR-01 会议纪要结构化输出与 Agent 状态。

依据 docs/architecture.md §5.2、§7：
- 所有缺失字段统一默认 "待确认"，不得编造。
- 每条待办附原文证据片段（evidence）。
"""

from typing import Any, Optional, TypedDict

from pydantic import BaseModel, Field

PENDING = "待确认"

VALID_TASK_TYPES = ("meeting_minutes", "weekly_report", "email_draft", "web_summary")


class ActionItem(BaseModel):
    """单条待办事项。"""

    task: str = Field(description="待办事项")
    owner: str = Field(default=PENDING, description="负责人")
    deadline: str = Field(default=PENDING, description="截止日期")
    evidence: str = Field(default="", description="原文证据片段")


class MeetingMinutes(BaseModel):
    """会议纪要结构化输出。"""

    date: str = Field(default=PENDING, description="会议日期")
    topic: str = Field(default=PENDING, description="会议主题")
    decisions: list[str] = Field(default_factory=list, description="关键决定")
    action_items: list[ActionItem] = Field(default_factory=list, description="待办列表")


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
    structured_result: Optional[MeetingMinutes] = None
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

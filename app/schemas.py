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


# 注：v1.3.0 线曾在此定义过一套简版 WeeklyReport / EmailDraft（period+work_done、
# subject+recipient+key_points）。v1.6.0 合并时按"本地 v1.4.0 设计为准"统一为本文件
# 后半部分的 EmailDraft / WeeklyReport（收件人列表 + 协议、进展/下周计划）。


# ---------- 邮件草稿（FR-03） ----------

# 发送协议：默认 draft_only（只出草稿，不发送），满足 Q-02 默认路径 100% 合规。
# 任何协议下，发送动作都必须由用户显式点击触发，生成流程绝不自动发送。
EMAIL_PROTOCOLS = ("draft_only", "mailto", "smtp")
DEFAULT_EMAIL_PROTOCOL = "draft_only"

PROTOCOL_LABELS = {
    "draft_only": "仅生成草稿（不发送，可复制/导出）",
    "mailto": "调用本机邮件客户端发送（mailto 链接）",
    "smtp": "由服务端 SMTP 直发（需在 .env 配置，且每封需显式确认）",
}


class EmailRecipient(BaseModel):
    """单个收件人：姓名来自纪要抽取，邮箱由用户手填，内容由系统生成。"""

    name: str = Field(description="姓名（来自会议纪要抽取的负责人）")
    email: str = Field(default="", description="邮箱；用户手动填写，系统绝不编造")
    content: str = Field(default="", description="发给该收件人的正文内容")
    send: bool = Field(default=False, description="是否发送（用户显式勾选，默认不发）")

    @property
    def email_valid(self) -> bool:
        """简易邮箱格式校验（仅用于前端提示与发送前拦截）。"""
        import re as _re

        return bool(_re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.email or ""))


class EmailDraft(BaseModel):
    """邮件草稿：发件人（本人）+ 协议 + 收件人列表。

    UI 规格（用户需求）：
      显示本人 → 协议 → 从会议纪要分析出的人数 → 名字/邮箱/内容/发送
    """

    sender: str = Field(default=PENDING, description="发件人（本人）；缺失显示待确认")
    protocol: str = Field(
        default=DEFAULT_EMAIL_PROTOCOL, description="发送协议，默认 draft_only（不发送）"
    )
    subject: str = Field(default="", description="邮件主题")
    greeting: str = Field(default="", description="称呼语（可按收件人替换）")
    recipients: list[EmailRecipient] = Field(
        default_factory=list, description="收件人列表，来自纪要中的负责人"
    )
    shared_body: str = Field(default="", description="公共正文（可编辑，作为各封邮件的基础）")
    source_topic: str = Field(default=PENDING, description="来源会议主题（溯源用）")
    source_date: str = Field(default=PENDING, description="来源会议日期（溯源用）")

    @property
    def recipient_count(self) -> int:
        return len(self.recipients)

    @property
    def selected_count(self) -> int:
        """已勾选发送的人数（0 表示仅保存草稿）。"""
        return sum(1 for r in self.recipients if r.send)


# ---------- 周报整理（FR-02） ----------


class WeeklyReportItem(BaseModel):
    """周报条目：保留 owner/deadline/evidence，便于零编造核对。"""

    content: str = Field(description="条目内容")
    owner: str = Field(default=PENDING, description="负责人")
    deadline: str = Field(default=PENDING, description="截止/计划时间")
    evidence: str = Field(default="", description="原文证据片段")


class WeeklyReport(BaseModel):
    """周报结构化输出：由会议纪要派生（决定→进展，待办→下周计划）。"""

    period: str = Field(default=PENDING, description="报告周期")
    topic: str = Field(default=PENDING, description="来源会议主题")
    progress: list[WeeklyReportItem] = Field(
        default_factory=list, description="本周进展（来自关键决定）"
    )
    next_plan: list[WeeklyReportItem] = Field(
        default_factory=list, description="下周计划（来自待办事项）"
    )
    risks: list[str] = Field(default_factory=list, description="风险与待确认项")
    source_date: str = Field(default=PENDING, description="来源会议日期（溯源用）")


# ---------- 派生（多 Skill 编排：理解 C） ----------


class DerivedTask(BaseModel):
    """可派生的下游任务（由当前产物推导，需用户点击才执行）。"""

    task_type: str = Field(description="可派生的任务类型")
    label: str = Field(description="按钮文案")
    reason: str = Field(default="", description="为何可派生（含人数/条目数等依据）")


class GenerateRequest(BaseModel):
    """POST /api/generate 请求体。"""

    input_text: str = Field(description="用户输入的原始文本")
    task_type: Optional[str] = Field(
        default=None,
        description="显式任务类型（点击提示按钮时由前端传入）；"
        "传入后锁定，不再依据正文关键词路由",
    )


class DeriveRequest(BaseModel):
    """POST /api/derive 请求体：基于已有会话产物派生下游任务（理解 C）。

    复用源会话已抽取的结构化结果，**不重新抽取**（省 token、保证一致）。
    """

    session_id: str = Field(description="源会话 id（会议纪要那一轮的 session_id）")
    task_type: str = Field(description="要派生的任务类型，如 email_draft / weekly_report")


class EmailSendRequest(BaseModel):
    """POST /api/email/send 请求体：显式发送（Q-02：必须用户点击触发）。"""

    session_id: str = Field(default="", description="关联会话 id（用于工具日志）")
    protocol: str = Field(default=DEFAULT_EMAIL_PROTOCOL, description="发送协议")
    sender: str = Field(default="", description="发件人显示名")
    subject: str = Field(default="", description="邮件主题")
    recipients: list[EmailRecipient] = Field(
        default_factory=list, description="仅 send=True 的收件人会被处理"
    )
    confirm: bool = Field(
        default=False, description="显式确认；False 时仅返回预览，不执行任何发送"
    )


class EmailSendResult(BaseModel):
    """单个收件人的发送结果。"""

    name: str
    email: str
    status: str = Field(description="sent / preview / skipped / failed")
    detail: str = ""


class EmailSendResponse(BaseModel):
    """POST /api/email/send 响应体。"""

    protocol: str
    dry_run: bool = Field(description="True 表示未真正发送（预览/草稿模式）")
    results: list[EmailSendResult] = Field(default_factory=list)
    error_message: str = ""


class ToolLog(BaseModel):
    """单次工具调用日志，用于前端工具状态面板与验收证据。"""

    tool_name: str
    status: str  # success / failed
    error_message: str = ""
    timestamp: str  # ISO 8601


class GenerateResponse(BaseModel):
    """POST /api/generate 响应体。

    多产物契约：`structured_result` 保留为会议纪要专用字段（向后兼容），
    其余 Skill 各用专有字段（email_draft / weekly_report / web_summary）；
    `derived_tasks` 承载"可继续派生"的建议（理解 C）。
    """

    session_id: str
    task_type: str
    routed_by: str  # explicit / keyword / llm / fallback_rule
    needs_clarification: bool = Field(
        default=False, description="路由置信度不足时为 True，前端应提示用户选择任务类型"
    )
    message: str = ""
    structured_result: Optional[MeetingMinutes] = None
    email_draft: Optional[EmailDraft] = None
    weekly_report: Optional[WeeklyReport] = None
    web_summary: Optional[WebSummary] = None
    derived_tasks: list[DerivedTask] = Field(
        default_factory=list,
        description="基于本次产物可继续生成的任务；需用户点击才执行（理解 C）",
    )
    derived_from: str = Field(default="", description="派生来源会话 id（空=本轮为原始输入）")
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


class ConfigUpdateRequest(BaseModel):
    """POST /api/config 请求体（Web 配置面板保存）。

    - `updates` 中**空字符串会被忽略**（视为"用户没改这一项"），
      避免前端表单留空时把已配置的 Key 抹掉；
    - 需要清空某键时放进 `clear`。
    """

    updates: dict[str, str] = Field(
        default_factory=dict, description="要写入的配置项；空值忽略"
    )
    clear: list[str] = Field(
        default_factory=list, description="需要清空的配置项（白名单内）"
    )


class ConfigTestRequest(BaseModel):
    """POST /api/config/test 请求体：测试模型连通性（可传未保存的值）。"""

    base_url: Optional[str] = Field(default=None, description="留空则用当前生效值")
    api_key: Optional[str] = Field(
        default=None, description="留空则用当前生效值；本地端点可不填"
    )
    model_name: Optional[str] = Field(default=None, description="留空则用当前生效值")
    timeout: float = Field(default=20.0, description="超时秒数")


class AuthRequest(BaseModel):
    """POST /api/auth/register 与 /api/auth/login 请求体。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class LogoutRequest(BaseModel):
    """POST /api/auth/logout 请求体。"""

    token: str


class ConversationCreateRequest(BaseModel):
    """POST /api/conversations 请求体。"""

    title: str = Field(default="新对话", max_length=64)


class ConversationUpdateRequest(BaseModel):
    """PUT /api/conversations/{id} 请求体：标题与轮次均可选更新。"""

    title: Optional[str] = Field(default=None, max_length=64)
    turns: Optional[list[dict[str, Any]]] = None


class AgentState(TypedDict, total=False):
    """LangGraph 工作流状态（architecture.md §5.2，ReAct 架构）。"""

    session_id: str
    task_type: str
    routed_by: str
    needs_clarification: bool
    input_text: str
    structured_result: dict[str, Any]
    email_draft: dict[str, Any]
    weekly_report: dict[str, Any]
    derived_tasks: list[dict[str, Any]]
    derived_from: str
    markdown: str
    edited_content: str
    tool_logs: list[dict[str, Any]]
    export_status: str  # pending / success / failed
    error_message: str
    # ReAct 循环新增字段
    react_messages: list[dict[str, Any]]  # OpenAI 多轮消息历史
    react_iteration: int  # 当前迭代轮数
    _react_fallback: bool  # 内部标记：LLM 不可用时走降级路径
    _tool_context: dict[str, Any]  # 跨工具临时上下文（如 web_fetch 的 page）

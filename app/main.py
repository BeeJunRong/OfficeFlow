"""FastAPI 入口（architecture.md §4.1）。

端点：
- POST /api/generate       触发生成（LangGraph 工作流）
- POST /api/derive         基于已有会话产物派生下游任务（多 Skill 编排·理解 C）
- POST /api/email/send     显式发送邮件（Q-02：必须用户点击触发，默认不发送）
- POST /api/export         显式点击导出 Markdown
- POST /api/retry          重试上一次失败的导出
- GET  /api/status/{id}    查询工具调用状态（验收证据）

安全约束（Q-02，默认路径 100% 满足）：
- 导出仅由用户显式请求触发，本服务不自动导出。
- 邮件默认协议 draft_only：生成流程**绝不**产生任何发送行为；
  真实发送必须同时满足 显式请求 + confirm=true + 协议=smtp + .env 显式开启。
- inject_failure 仅用于测试环境受控错误注入。
"""

import uuid
from typing import Any

# 必须最先执行：app.storage 在导入时读取 OFFICEFLOW_DB_PATH 等环境变量，
# 因此 .env 要在任何业务模块 import 之前加载。
from app.env import load_env

load_env()

from fastapi import FastAPI, Header, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402

from app.auth import AuthError, get_auth_store  # noqa: E402
from app.conversations import get_conv_store  # noqa: E402
from app.derive import compute_derived_tasks  # noqa: E402
from app.graph import run_workflow  # noqa: E402
from app.schemas import (  # noqa: E402
    VALID_TASK_TYPES,
    AuthRequest,
    ConfigTestRequest,
    ConfigUpdateRequest,
    ConversationCreateRequest,
    ConversationUpdateRequest,
    DeriveRequest,
    EmailDraft,
    EmailSendRequest,
    EmailSendResponse,
    ExportRequest,
    ExportResponse,
    GenerateRequest,
    GenerateResponse,
    LogoutRequest,
    MeetingMinutes,
    RetryRequest,
    ToolLog,
    WebSummary,
    WeeklyReport,
)
from app.storage import get_store  # noqa: E402
from app.tools.email_send import process_send  # noqa: E402
from app.tools.export_markdown import export_markdown  # noqa: E402

app = FastAPI(title="OfficeFlow", version="1.6.0")

# Streamlit 前端独立端口运行，需要跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 允许派生的来源 → 目标（与 app/derive.py 的规则保持一致，此处用于入口校验）
DERIVABLE_FROM: dict[str, set[str]] = {
    "meeting_minutes": {"email_draft", "weekly_report"},
    "weekly_report": {"email_draft"},
    "email_draft": set(),
    "web_summary": set(),
}


# ---------- 内部辅助 ----------


def _try_model(model: Any, raw: Any) -> Any:
    """把状态中的 dict 产物解析为 Pydantic 模型（解析失败按"无产物"处理）。"""
    if not raw:
        return None
    try:
        return model(**raw)
    except Exception:  # noqa: BLE001 - 结构不符时按"无产物"处理
        return None


def _parse_models(state: dict[str, Any]) -> tuple[
    MeetingMinutes | None, WeeklyReport | None, EmailDraft | None
]:
    """把 state 中的 dict 产物解析为 Pydantic 模型（解析失败则该产物为 None）。

    网页整理的产物同样是放在 `structured_result` 里的 dict，但不是会议纪要，
    必须按 task_type 排除，否则会被 MeetingMinutes 静默吞成一份空纪要。
    """
    raw_minutes = None if state.get("task_type") == "web_summary" else state.get("structured_result")
    return (
        _try_model(MeetingMinutes, raw_minutes),
        _try_model(WeeklyReport, state.get("weekly_report")),
        _try_model(EmailDraft, state.get("email_draft")),
    )


def _parse_web_summary(state: dict[str, Any]) -> WebSummary | None:
    """网页整理产物（FR-04）：仅当本轮任务类型为 web_summary 时解析。"""
    if state.get("task_type") != "web_summary":
        return None
    return _try_model(WebSummary, state.get("structured_result"))


def _primary_output(state: dict[str, Any]) -> dict[str, Any] | None:
    """按任务类型取"主产物"，用于落库与回传。"""
    task_type = state.get("task_type")
    if task_type == "weekly_report":
        return state.get("weekly_report")
    if task_type == "email_draft":
        return state.get("email_draft")
    return state.get("structured_result")


def _compute_derived(state: dict[str, Any]) -> list[Any]:
    """计算可派生任务；仅在有主产物且未出错时给出建议。"""
    if state.get("error_message") or state.get("needs_clarification"):
        return []
    minutes, report, draft = _parse_models(state)
    if minutes is None and report is None and draft is None:
        return []
    return compute_derived_tasks(
        state.get("task_type", ""), minutes=minutes, report=report, draft=draft
    )


def _persist(state: dict[str, Any], session_id: str, input_text: str) -> None:
    """落库：会话、工具日志、主产物。"""
    store = get_store()
    store.save_session(session_id, state.get("task_type", ""), input_text)
    for entry in state.get("tool_logs", []):
        store.add_tool_log(
            session_id,
            entry["tool_name"],
            entry["status"],
            entry.get("error_message", ""),
        )
    primary = _primary_output(state)
    if primary:
        store.save_result(session_id, primary, state.get("export_status", "pending"))


def _to_response(
    state: dict[str, Any], session_id: str, derived_from: str = ""
) -> GenerateResponse:
    minutes, report, draft = _parse_models(state)
    return GenerateResponse(
        session_id=session_id,
        task_type=state.get("task_type", ""),
        routed_by=state.get("routed_by", ""),
        needs_clarification=state.get("needs_clarification", False),
        message=state.get("error_message", ""),
        structured_result=minutes,
        email_draft=draft,
        weekly_report=report,
        web_summary=_parse_web_summary(state),
        derived_tasks=_compute_derived(state),
        derived_from=derived_from,
        markdown=state.get("markdown", ""),
        tool_logs=[ToolLog(**log) for log in state.get("tool_logs", [])],
        error_message=state.get("error_message", ""),
        reasoning_trace=state.get("reasoning_trace", []),
        react_iteration=state.get("react_iteration", 0),
        outputs=state.get("outputs", []),
    )


# ---------- 端点 ----------


# ---------- 认证（登录/注册/登出） ----------


def _auth_user(authorization: str) -> dict:
    """从 Authorization: Bearer <token> 解析当前用户；无效抛 401。"""
    token = authorization.removeprefix("Bearer ").strip()
    user = get_auth_store().user_for_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已过期，请重新登录")
    return user


@app.post("/api/auth/register")
def auth_register(req: AuthRequest) -> dict:
    """注册新用户（用户名 2-32 字符、密码 ≥6 位；口令 PBKDF2 散列存储）。"""
    try:
        user = get_auth_store().register(req.username, req.password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return {"ok": True, "username": user["username"]}


@app.post("/api/auth/login")
def auth_login(req: AuthRequest) -> dict:
    """登录：校验口令后签发 7 天有效的 Bearer 令牌。"""
    try:
        user = get_auth_store().verify(req.username, req.password)
    except AuthError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    token = get_auth_store().issue_token(user["id"])
    return {"ok": True, "token": token, "username": user["username"]}


@app.post("/api/auth/logout")
def auth_logout(req: LogoutRequest) -> dict:
    """登出：吊销令牌。"""
    get_auth_store().revoke_token(req.token)
    return {"ok": True}


# ---------- 对话管理（登录后，按用户隔离） ----------


@app.get("/api/conversations")
def list_conversations(authorization: str = Header(default="")) -> list[dict]:
    """当前用户的对话列表（最近更新在前，不含轮次正文）。"""
    user = _auth_user(authorization)
    return get_conv_store().list(user["id"])


@app.post("/api/conversations")
def create_conversation(
    req: ConversationCreateRequest, authorization: str = Header(default="")
) -> dict:
    """新建对话。"""
    user = _auth_user(authorization)
    return get_conv_store().create(user["id"], req.title)


@app.get("/api/conversations/{conv_id}")
def get_conversation(conv_id: str, authorization: str = Header(default="")) -> dict:
    """取单个对话（含完整轮次），供切换对话时恢复。"""
    user = _auth_user(authorization)
    conv = get_conv_store().get(conv_id, user["id"])
    if conv is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conv


@app.put("/api/conversations/{conv_id}")
def update_conversation(
    conv_id: str,
    req: ConversationUpdateRequest,
    authorization: str = Header(default=""),
) -> dict:
    """更新对话标题和/或轮次（前端每次生成后整存整取）。"""
    user = _auth_user(authorization)
    conv = get_conv_store().update(conv_id, user["id"], req.title, req.turns)
    if conv is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return conv


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, authorization: str = Header(default="")) -> dict:
    """删除对话。"""
    user = _auth_user(authorization)
    if not get_conv_store().delete(conv_id, user["id"]):
        raise HTTPException(status_code=404, detail="对话不存在")
    return {"ok": True}


def _seed_from_history(history: list[dict[str, Any]] | None) -> dict[str, Any]:
    """从历史对话恢复上一轮结构化产物（跨轮"接着做"的关键）。

    前端 history 中的 assistant 条目带 session_id；这里从 SQLite 取回该轮
    主产物，按类型放回 AgentState。这样"接着整理成周报/发邮件"时，
    Skill 与 ReAct 都能直接复用真实纪要，而不是让模型凭摘要重写。
    """
    store = get_store()
    for turn in reversed(history or []):
        if turn.get("role") != "assistant":
            continue
        session_id = turn.get("session_id") or ""
        if not session_id:
            continue
        stored = store.get_structured_result(session_id)
        if not stored:
            continue
        task_type = turn.get("task_type") or ""
        if task_type == "weekly_report":
            return {"weekly_report": stored, "seed_from": session_id}
        if task_type == "email_draft":
            return {"email_draft": stored, "seed_from": session_id}
        # meeting_minutes / web_summary 的主产物都放在 structured_result
        return {"structured_result": stored, "seed_from": session_id}
    return {}


@app.post("/api/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """触发生成：空输入阻止；路由→抽取→校验→渲染。

    自动识别模式下，历史对话中上一轮的结构化产物会先注入状态，
    供"接着整理成周报/生成邮件"等跨轮指令复用（不重新抽取）。
    """
    if not req.input_text or not req.input_text.strip():
        raise HTTPException(status_code=400, detail="请输入非空会议文字记录")

    session_id = uuid.uuid4().hex[:12]
    state = run_workflow(
        {
            "session_id": session_id,
            "input_text": req.input_text,
            "task_type": req.task_type,
            "history": req.history or [],
            "tool_logs": [],
            **_seed_from_history(req.history),
        }
    )
    _persist(state, session_id, req.input_text)
    return _to_response(state, session_id)


@app.post("/api/derive", response_model=GenerateResponse)
def derive(req: DeriveRequest) -> GenerateResponse:
    """基于已有会话产物派生下游任务（理解 C）。

    - 复用源会话的结构化结果，**不重新抽取**（省 token，且与源产物严格一致）；
    - 只允许有数据依据的派生方向（见 DERIVABLE_FROM）；
    - 派生同样只是一次普通生成，不产生任何对外副作用（不发送、不导出）。
    """
    if req.task_type not in VALID_TASK_TYPES:
        raise HTTPException(status_code=400, detail=f"未知任务类型：{req.task_type}")

    store = get_store()
    src = store.get_session(req.session_id)
    if not src:
        raise HTTPException(
            status_code=404,
            detail=f"找不到会话 {req.session_id}，无法派生（请先完成一次生成）",
        )

    src_type = src.get("task_type") or ""
    if req.task_type not in DERIVABLE_FROM.get(src_type, set()):
        allowed = "、".join(sorted(DERIVABLE_FROM.get(src_type, set()))) or "无"
        raise HTTPException(
            status_code=400,
            detail=f"不支持从 {src_type or '未知类型'} 派生 {req.task_type}（可派生：{allowed}）",
        )

    source_output = store.get_structured_result(req.session_id)
    if not source_output:
        raise HTTPException(
            status_code=409,
            detail="源会话没有可复用的结构化结果（可能该轮生成失败），请重新生成后再派生",
        )

    new_id = uuid.uuid4().hex[:12]
    # 关键：源产物按类型放回正确的状态键，Skill 据此走"派生"分支而不重新抽取
    seed: dict[str, Any] = dict(source_output)
    state = run_workflow(
        {
            "session_id": new_id,
            "input_text": src.get("input_text", ""),
            "task_type": req.task_type,
            "structured_result": seed if src_type == "meeting_minutes" else None,
            "weekly_report": seed if src_type == "weekly_report" else None,
            "derived_from": req.session_id,
            "tool_logs": [],
        }
    )
    _persist(state, new_id, src.get("input_text", ""))
    return _to_response(state, new_id, derived_from=req.session_id)


@app.post("/api/email/send", response_model=EmailSendResponse)
def email_send(req: EmailSendRequest) -> EmailSendResponse:
    """显式发送邮件（Q-02：必须用户点击触发）。

    默认协议 draft_only → 只返回预览，不发送。
    真实发送需同时满足：协议=smtp、confirm=true、.env 配置齐全且显式开启发送开关。
    """
    store = get_store()
    if req.session_id:
        # 记录到工具日志，作为"发送由用户显式触发"的验收证据
        store.add_tool_log(
            req.session_id,
            "email_send",
            "success",
            f"协议={req.protocol} confirm={req.confirm} 勾选={sum(1 for r in req.recipients if r.send)}",
        )
    return process_send(
        protocol=req.protocol,
        sender=req.sender,
        subject=req.subject,
        recipients=req.recipients,
        confirm=req.confirm,
    )


def _do_export(session_id: str, req: ExportRequest) -> ExportResponse:
    """导出与重试共用逻辑：写库、调用导出工具、记录导出记录。"""
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="导出内容为空")
    success, path, error = export_markdown(
        req.content, req.filename, inject_failure=req.inject_failure
    )
    get_store().add_export_record(session_id, path if success else "", "success" if success else "failed", error)
    return ExportResponse(success=success, file_path=path, error_message=error)


@app.post("/api/export", response_model=ExportResponse)
def export_md(req: ExportRequest) -> ExportResponse:
    """用户显式点击"导出 .md"触发；失败保留内容，可重试。"""
    return _do_export(req.session_id, req)


@app.post("/api/retry", response_model=ExportResponse)
def retry_export(req: RetryRequest) -> ExportResponse:
    """重试上一次失败的导出（E1/E3 场景）。"""
    return _do_export(req.session_id, req)


@app.get("/api/status/{session_id}")
def status(session_id: str) -> dict:
    """查询工具调用状态与导出记录（前端工具状态面板 + 验收证据）。"""
    return get_store().get_status(session_id)


@app.get("/api/download")
def download(file_path: str) -> FileResponse:
    """下载导出的 .md 文件（仅限 exports 目录内，防路径穿越）。"""
    from pathlib import Path

    from app.tools.export_markdown import EXPORT_DIR

    target = Path(file_path).resolve()
    if not str(target).startswith(str(EXPORT_DIR.resolve())):
        raise HTTPException(status_code=403, detail="非法文件路径")
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target, filename=target.name, media_type="text/markdown")


@app.get("/api/email/protocols")
def email_protocols() -> dict:
    """前端展示用：可用发送协议、当前协议能力与是能否真实发送。"""
    from app.schemas import PROTOCOL_LABELS
    from app.tools.email_send import smtp_config

    cfg = smtp_config()
    return {
        "default": "draft_only",
        "protocols": [{"value": k, "label": v} for k, v in PROTOCOL_LABELS.items()],
        "smtp_available": cfg["available"],
        "smtp_enabled": cfg["enabled"],
        "smtp_missing": cfg["missing"],
        "note": "任何协议下发送都必须由用户显式点击触发；生成流程不会发送邮件。",
    }


# ---------- 配置（Web 配置面板） ----------


@app.get("/api/config")
def get_config() -> dict:
    """读取当前配置。密钥只返回掩码，**绝不回传明文**。"""
    from app.config_store import public_view

    return public_view()


@app.post("/api/config")
def save_config(req: ConfigUpdateRequest) -> dict:
    """保存配置到 .env，并尽量立即生效。

    - 白名单外的键会被拒绝（400）；
    - 空字符串视为"未修改"，只有 `clear` 里列出的键才会被清空；
    - 模型相关配置写入后重置 LLM 单例，**无需重启后端**；
    - 导出目录 / 数据库路径在 import 时读取，需重启后端（返回值会列出）。
    """
    from app.config_store import write_config

    try:
        return write_config(req.updates, req.clear)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/config/test")
def test_config(req: ConfigTestRequest) -> dict:
    """测试模型连通性。可传未保存的值，支持"先测再存"。"""
    from app.config_store import test_model

    return test_model(
        base_url=req.base_url,
        api_key=req.api_key,
        model_name=req.model_name,
        timeout=req.timeout,
    )


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}

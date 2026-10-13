"""FastAPI 入口（architecture.md §4.1）。

端点：
- POST /api/generate       触发纪要生成（LangGraph 工作流）
- POST /api/export         显式点击导出 Markdown
- POST /api/retry          重试上一次失败的导出
- GET  /api/status/{id}    查询工具调用状态（验收证据）

安全约束（Q-02，100% 满足）：
- 导出仅由用户显式请求触发，本服务不自动导出。
- inject_failure 仅用于测试环境受控错误注入。
"""

import uuid

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.graph import run_workflow
from app.schemas import (
    ExportRequest,
    ExportResponse,
    GenerateRequest,
    GenerateResponse,
    MeetingMinutes,
    RetryRequest,
    ToolLog,
)
from app.storage import get_store
from app.tools.export_markdown import export_markdown

app = FastAPI(title="OfficeFlow", version="0.1.0")

# Streamlit 前端独立端口运行，需要跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """触发纪要生成：空输入阻止；路由→抽取→校验→Markdown。"""
    if not req.input_text or not req.input_text.strip():
        raise HTTPException(status_code=400, detail="请输入非空会议文字记录")

    session_id = uuid.uuid4().hex[:12]
    state = run_workflow(
        {
            "session_id": session_id,
            "input_text": req.input_text,
            "task_type": req.task_type,
            "tool_logs": [],
        }
    )

    store = get_store()
    store.save_session(session_id, state.get("task_type", ""), req.input_text)
    for entry in state.get("tool_logs", []):
        store.add_tool_log(
            session_id,
            entry["tool_name"],
            entry["status"],
            entry.get("error_message", ""),
        )
    if state.get("structured_result"):
        store.save_result(
            session_id, state["structured_result"], state.get("export_status", "pending")
        )

    structured = None
    if state.get("structured_result"):
        try:
            structured = MeetingMinutes(**state["structured_result"])
        except Exception:  # noqa: BLE001
            structured = None

    return GenerateResponse(
        session_id=session_id,
        task_type=state.get("task_type", ""),
        routed_by=state.get("routed_by", ""),
        needs_clarification=state.get("needs_clarification", False),
        message=state.get("error_message", ""),
        structured_result=structured,
        markdown=state.get("markdown", ""),
        tool_logs=[ToolLog(**log) for log in state.get("tool_logs", [])],
        error_message=state.get("error_message", ""),
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


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}

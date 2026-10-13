"""OfficeFlow 前端工作台（Streamlit 原型，architecture.md §3）。

- 统一搜索框 + 四个任务提示按钮（点击填充模板并锁定 task_type）
- 结果展示与编辑、工具状态面板、导出按钮、异常提示与重试
- 状态保存在 st.session_state，刷新不要求恢复历史
"""

import os

import httpx
import streamlit as st

BACKEND = os.getenv("OFFICEFLOW_BACKEND", "http://localhost:8000")

PROMPTS = {
    "会议纪要": {
        "task_type": "meeting_minutes",
        "template": "请帮我整理会议纪要：\n[粘贴会议记录]",
    },
    "周报整理": {
        "task_type": "weekly_report",
        "template": "请帮我整理周报：\n[粘贴本周工作记录]",
    },
    "邮件草稿": {
        "task_type": "email_draft",
        "template": "请帮我写邮件草稿：\n[说明邮件背景与要点]",
    },
    "网页整理": {
        "task_type": "web_summary",
        "template": "请帮我整理网页内容：\n[粘贴网页文字]",
    },
}

st.set_page_config(page_title="OfficeFlow", page_icon="📋", layout="wide")


def _export(session_id: str, content: str, inject_failure: bool) -> tuple[bool, str]:
    """调用后端导出接口；返回 (success, file_path 或 error_message)。"""
    try:
        resp = httpx.post(
            f"{BACKEND}/api/export",
            json={
                "session_id": session_id,
                "content": content,
                "filename": f"meeting_minutes_{session_id}",
                "inject_failure": inject_failure,
            },
            timeout=60,
        ).json()
    except httpx.HTTPError as exc:
        return False, f"导出失败：后端不可用（{exc}）"
    if resp.get("success"):
        return True, resp["file_path"]
    return False, resp.get("error_message", "导出失败，可重试")

st.title("OfficeFlow 办公信息整理")
st.caption("单 Agent + 多工具 ｜ 首版完整支持：会议纪要（FR-01）")

# ---- 会话状态 ----
ss = st.session_state
ss.setdefault("input_text", "")
ss.setdefault("locked_task_type", None)  # 显式点击锁定
ss.setdefault("session_id", "")
ss.setdefault("task_type", "")
ss.setdefault("routed_by", "")
ss.setdefault("markdown", "")
ss.setdefault("edited", "")
ss.setdefault("tool_logs", [])
ss.setdefault("error", "")
ss.setdefault("export_result", None)  # (success, path/error)
ss.setdefault("needs_clarification", False)

# ---- 统一搜索框 + 提示按钮 ----
input_text = st.text_area(
    "统一输入框",
    value=ss.input_text,
    height=220,
    placeholder="在此输入或粘贴内容，例如：帮我做会议纪要：2026年10月9日项目例会……",
)

cols = st.columns(4)
for col, (label, conf) in zip(cols, PROMPTS.items()):
    if col.button(label, use_container_width=True):
        ss.input_text = conf["template"]
        ss.locked_task_type = conf["task_type"]
        ss.needs_clarification = False
        st.rerun()

if ss.locked_task_type:
    st.info(f"已锁定任务类型：**{ss.locked_task_type}**（显式点击优先，正文关键词不影响路由）")

# ---- 生成 ----
left, right = st.columns([1, 3])
if left.button("生成", type="primary", disabled=not input_text.strip()):
    ss.input_text = input_text
    try:
        with st.spinner("Agent 正在处理……"):
            resp = httpx.post(
                f"{BACKEND}/api/generate",
                json={
                    "input_text": input_text,
                    "task_type": ss.locked_task_type,
                },
                timeout=120,
            ).json()
        if resp.get("detail"):
            ss.error = resp["detail"]
        else:
            ss.session_id = resp["session_id"]
            ss.task_type = resp["task_type"]
            ss.routed_by = resp["routed_by"]
            ss.markdown = resp["markdown"]
            ss.edited = resp["markdown"]
            ss.tool_logs = [log for log in resp.get("tool_logs", [])]
            ss.error = resp.get("error_message", "")
            ss.needs_clarification = resp.get("needs_clarification", False)
            ss.export_result = None
    except httpx.HTTPError as exc:
        ss.error = f"后端不可用：{exc}，请确认后端已启动后重试"
    st.rerun()

if right.button("清空", use_container_width=False):
    for key in (
        "input_text", "locked_task_type", "session_id", "task_type",
        "markdown", "edited", "tool_logs", "error", "export_result",
        "needs_clarification",
    ):
        ss[key] = "" if isinstance(ss[key], str) else (None if key == "locked_task_type" or key == "export_result" else ([] if key == "tool_logs" else False))
    st.rerun()

if ss.needs_clarification:
    st.warning("无法确定任务类型，请点击上方任务按钮选择（会议纪要 / 周报整理 / 邮件草稿 / 网页整理）")
if ss.error:
    st.error(ss.error)

# ---- 结果展示与编辑 ----
if ss.edited:
    st.divider()
    st.subheader(f"生成结果（任务类型：{ss.task_type or '未确定'}）")
    edited = st.text_area("可编辑纪要（导出内容以此为准）", value=ss.edited, height=360)
    if st.button("保存编辑"):
        ss.edited = edited
        st.rerun()

    ec1, ec2 = st.columns(2)
    if ec1.button("导出 .md", type="primary"):
        ss.export_result = _export(ss.session_id, edited, inject_failure=False)
        st.rerun()
    if ec2.button("重试导出", disabled=not (ss.export_result and not ss.export_result[0])):
        ss.export_result = _export(ss.session_id, edited, inject_failure=False)
        st.rerun()

    if ss.export_result is not None:
        success, info = ss.export_result
        if success:
            st.success(f"导出成功：{info}")
        else:
            st.error(f"{info}，页面内容已保留，可点击「重试导出」")

# ---- 工具状态面板 ----
if ss.tool_logs:
    st.divider()
    st.subheader("工具调用状态")
    for log in ss.tool_logs:
        status_icon = "✅" if log["status"] == "success" else "❌"
        detail = f"：{log['error_message']}" if log.get("error_message") else ""
        st.markdown(
            f"- {status_icon} `{log['tool_name']}` — {log['status']} — {log['timestamp']}{detail}"
        )

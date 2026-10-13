"""OfficeFlow 前端工作台：ChatGPT 风格对话式界面。

交互形态（用户要求）：
- 空状态：页面中央只有一个搜索框 + 四个任务提示按钮（类似 ChatGPT 首页）。
- 提交后：搜索框沉到底部固定（st.chat_input），中央区域变成对话流。
- 对话流中逐步展示 Agent 调用了什么工具（路由 → 抽取 → 校验 → 生成），
  让用户看到一步步产出最终结果的原理；会议纪要/周报/邮件/网页整理同一套流程。
- 每轮结果可编辑、可导出/重试；导出仅由用户显式点击触发（Q-02）。
- 状态保存在 st.session_state，刷新不要求恢复历史（architecture.md §3.2）。
"""

import os
import time

import httpx
import streamlit as st

BACKEND = os.getenv("OFFICEFLOW_BACKEND", "http://localhost:8000")

TASKS = [
    ("会议纪要", "meeting_minutes"),
    ("周报整理", "weekly_report"),
    ("邮件草稿", "email_draft"),
    ("网页整理", "web_summary"),
]

# 工具名 → 人话说明（前端过程展示用）
TOOL_LABELS = {
    "route": "识别任务类型（Skill 路由）",
    "extract_fields": "调用结构化抽取工具（日期/主题/决定/待办）",
    "validate": "调用零编造校验工具（无依据字段回退「待确认」）",
    "render_markdown": "调用 Markdown 生成工具",
    "export_markdown": "调用导出工具",
}

st.set_page_config(page_title="OfficeFlow", page_icon="📋", layout="centered")

# ---- 全局样式：隐藏 Streamlit 附件元素，接近 ChatGPT 的干净页面 ----
st.markdown(
    """
    <style>
      #MainMenu, footer, [data-testid="stStatusWidget"] {visibility: hidden;}
      .block-container {padding-top: 2.5rem; padding-bottom: 7rem; max-width: 760px;}
      [data-testid="stChatInput"] {max-width: 760px;}
      .of-center {text-align: center;}
      .of-sub {color: #888; text-align: center; margin-top: -0.5rem;}
      .of-spacer {height: 16vh;}
    </style>
    """,
    unsafe_allow_html=True,
)

# ---- 会话状态 ----
ss = st.session_state
ss.setdefault("turns", [])            # 每轮：{user, response, animated, export_result}
ss.setdefault("locked_task_type", None)  # 显式点击锁定的任务类型
ss.setdefault("draft", "")            # 空状态居中搜索框的草稿


# ---- 后端调用 ----

def _generate(text: str, task_type: str | None) -> dict:
    """调用 /api/generate；后端不可用时构造等价的错误响应（内容保留）。"""
    try:
        resp = httpx.post(
            f"{BACKEND}/api/generate",
            json={"input_text": text, "task_type": task_type},
            timeout=120,
        ).json()
        return resp if "detail" not in resp else {
            "session_id": "", "task_type": task_type or "", "routed_by": "",
            "needs_clarification": False, "markdown": "", "tool_logs": [],
            "error_message": resp["detail"],
        }
    except httpx.HTTPError as exc:
        return {
            "session_id": "", "task_type": task_type or "", "routed_by": "",
            "needs_clarification": False, "markdown": "", "tool_logs": [],
            "error_message": f"后端不可用：{exc}，请确认后端已启动后重试",
        }


def _export(session_id: str, content: str) -> tuple[bool, str]:
    """调用 /api/export（用户显式点击触发）；返回 (success, 路径或错误)。"""
    try:
        resp = httpx.post(
            f"{BACKEND}/api/export",
            json={
                "session_id": session_id,
                "content": content,
                "filename": f"officeflow_{session_id or 'local'}",
            },
            timeout=60,
        ).json()
    except httpx.HTTPError as exc:
        return False, f"导出失败：后端不可用（{exc}），可重试"
    if resp.get("success"):
        return True, resp["file_path"]
    return False, resp.get("error_message", "导出失败，可重试")


def _submit(text: str) -> None:
    """提交一轮：用户消息 → 后端生成 → 追加到对话流。"""
    response = _generate(text, ss.locked_task_type)
    ss.turns.append(
        {
            "user": text,
            "response": response,
            "animated": False,     # 首次渲染时逐步展示工具步骤
            "export_result": None, # (success, path/error)
        }
    )


# ---- 组件 ----

def _render_tool_log(log: dict, delay: float | None = None) -> None:
    """渲染单个工具调用步骤；delay 提供时先显示运行中再落定（逐步呈现）。"""
    label = TOOL_LABELS.get(log["tool_name"], f"调用工具 {log['tool_name']}")
    detail = log.get("error_message") or ""
    ok = log["status"] == "success"
    target = st.empty() if delay else st
    if delay:
        target.status(f"{label}……", state="running", expanded=False)
        time.sleep(delay)
    with target.status(
        f"{'✅' if ok else '❌'} {label}",
        state="complete" if ok else "error",
        expanded=not ok,
    ):
        st.caption(f"工具：{log['tool_name']} · 状态：{log['status']} · {log['timestamp']}")
        if detail:
            st.caption(detail)


def _render_turn(idx: int) -> None:
    """渲染一轮对话：用户消息 + 助手的工具步骤与可编辑结果。"""
    turn = ss.turns[idx]
    resp = turn["response"]

    with st.chat_message("user"):
        st.markdown(turn["user"])

    with st.chat_message("assistant"):
        if resp.get("task_type"):
            st.caption(
                f"任务类型：**{resp['task_type']}** · 路由方式：{resp['routed_by']}"
            )

        # 工具调用过程：首轮逐步呈现，之后直接静态展示
        logs = resp.get("tool_logs", [])
        if logs:
            st.markdown("**Agent 执行过程**")
            for log in logs:
                _render_tool_log(log, delay=0.45 if not turn["animated"] else None)
        turn["animated"] = True

        if resp.get("needs_clarification"):
            st.warning("无法确定任务类型，请点击下方任务按钮选择后重试。")
            return
        if resp.get("error_message"):
            st.error(resp["error_message"])

        # 生成结果：可编辑（导出内容以此为准）
        markdown = resp.get("markdown", "")
        if not markdown:
            return
        if resp["task_type"] == "meeting_minutes":
            st.markdown("**生成结果**（可编辑，导出以此为准）")
            turn["edited"] = st.text_area(
                "生成结果",
                value=turn.get("edited", markdown),
                height=320,
                label_visibility="collapsed",
                key=f"edit_{idx}",
            )
            c1, c2 = st.columns(2)
            if c1.button("导出 .md", key=f"export_{idx}", type="primary"):
                turn["export_result"] = _export(resp["session_id"], turn["edited"])
                st.rerun()
            prev = turn["export_result"]
            if c2.button(
                "重试导出",
                key=f"retry_{idx}",
                disabled=not (prev and not prev[0]),
            ):
                turn["export_result"] = _export(resp["session_id"], turn["edited"])
                st.rerun()
            if turn["export_result"] is not None:
                ok, info = turn["export_result"]
                if ok:
                    st.success(f"导出成功：{info}")
                else:
                    st.error(f"{info}，内容已保留，可点击「重试导出」")
        else:
            st.info(markdown)


def _task_chips() -> None:
    """任务提示按钮：显式点击锁定任务类型（FR-00 显式优先）。"""
    cols = st.columns(len(TASKS) + 1)
    for col, (label, task_type) in zip(cols, TASKS):
        active = ss.locked_task_type == task_type
        if col.button(
            f"{'●' if active else ''}{label}".lstrip(),
            key=f"chip_{task_type}",
            use_container_width=True,
        ):
            ss.locked_task_type = task_type
            st.rerun()
    if cols[-1].button("自动识别", key="chip_auto", use_container_width=True):
        ss.locked_task_type = None
        st.rerun()


# ---- 页面 ----

if not ss.turns:
    # 空状态：居中搜索框（类似 ChatGPT 首页）
    st.markdown("<div class='of-spacer'></div>", unsafe_allow_html=True)
    st.markdown("<h1 class='of-center'>OfficeFlow</h1>", unsafe_allow_html=True)
    st.markdown(
        "<p class='of-sub'>会议纪要 · 周报整理 · 邮件草稿 · 网页整理<br>"
        "粘贴文字，剩下的交给 Agent —— 执行过程全程可见</p>",
        unsafe_allow_html=True,
    )

    draft = st.text_area(
        "搜索框",
        value=ss.draft,
        height=180,
        placeholder="在此输入或粘贴内容，例如：帮我做会议纪要：2026年10月9日项目例会……",
        label_visibility="collapsed",
    )
    ss.draft = draft

    _task_chips()
    if ss.locked_task_type:
        st.caption(f"已选择任务类型：**{ss.locked_task_type}**（显式点击优先，正文关键词不影响路由）")

    _, center, _ = st.columns([2, 1, 2])
    if center.button("开始处理", type="primary", use_container_width=True,
                     disabled=not draft.strip()):
        text = draft.strip()
        ss.draft = ""
        _submit(text)
        st.rerun()  # 切换到对话模式，搜索框沉底
else:
    # 对话模式：顶部标题 + 新建对话
    tcol, ncol = st.columns([4, 1])
    tcol.markdown("#### 📋 OfficeFlow")
    if ncol.button("新建对话", use_container_width=True):
        ss.turns = []
        st.rerun()

    # 对话流（先渲染历史，再处理新输入）
    for idx in range(len(ss.turns)):
        _render_turn(idx)

    # 底部：任务提示按钮 + 固定的输入框（st.chat_input 自动沉底）
    _task_chips()
    if ss.locked_task_type:
        st.caption(f"当前任务类型：**{ss.locked_task_type}**（点击上方按钮可切换，或选「自动识别」）")
    user_text = st.chat_input("输入内容，粘贴后回车发送……")
    if user_text and user_text.strip():
        _submit(user_text.strip())
        st.rerun()

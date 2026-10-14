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
import sys
import time
from pathlib import Path

import httpx
import streamlit as st

# 载入项目根的 .env（BACKEND 在下方模块级读取，必须先加载）。
#
# ⚠️ 必须用同目录的 env_loader，**不要**写 `from app.env import load_env`：
# 本文件名为 app.py，会被 Python/Streamlit 注册为顶层模块 `app`，
# 于是 `from app.xxx import ...` 会解析到脚本自身，导致
#   - "No module named 'app.env'; 'app' is not a package"，或
#   - 脚本被反复重新执行 → RecursionError: maximum recursion depth exceeded
# 前端入口脚本因此不得导入 app 包，只依赖纯标准库的 env_loader。
sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_loader import load_env  # noqa: E402

load_env()

BACKEND = os.getenv("OFFICEFLOW_BACKEND", "http://localhost:8001")

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
      /* Put the user's avatar and message on the right; keep Agent messages on the left. */
      [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
        flex-direction: row-reverse;
      }
      [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"])
        [data-testid="stChatMessageContent"] {
        text-align: right;
      }
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


def _export(
    session_id: str, content: str, filename: str | None = None
) -> tuple[bool, str]:
    """调用 /api/export（用户显式点击触发）；返回 (success, 路径或错误)。"""
    try:
        resp = httpx.post(
            f"{BACKEND}/api/export",
            json={
                "session_id": session_id,
                "content": content,
                "filename": filename or f"officeflow_{session_id or 'local'}",
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
            "send_result": None,   # 邮件发送结果
        }
    )


def _derive(source_session_id: str, task_type: str, label: str) -> None:
    """派生下游任务（理解 C）：复用源会话产物，不重新抽取。

    失败时也追加一轮以便用户看到原因（内容不丢、可重试）。
    """
    try:
        response = httpx.post(
            f"{BACKEND}/api/derive",
            json={"session_id": source_session_id, "task_type": task_type},
            timeout=120,
        ).json()
        if "detail" in response:
            response = {
                "session_id": "", "task_type": task_type, "routed_by": "derive",
                "needs_clarification": False, "derived_tasks": [],
                "markdown": "", "tool_logs": [],
                "error_message": str(response["detail"]),
            }
    except httpx.HTTPError as exc:
        response = {
            "session_id": "", "task_type": task_type, "routed_by": "derive",
            "needs_clarification": False, "derived_tasks": [],
            "markdown": "", "tool_logs": [],
            "error_message": f"后端不可用：{exc}",
        }
    ss.turns.append(
        {
            "user": f"（基于上一轮结果）{label}",
            "response": response,
            "animated": False,
            "export_result": None,
            "send_result": None,
        }
    )


def _email_send(payload: dict) -> dict:
    """调用显式发送端点。默认协议 draft_only → 后端只返回预览，不发送。"""
    try:
        return httpx.post(f"{BACKEND}/api/email/send", json=payload, timeout=120).json()
    except httpx.HTTPError as exc:
        return {
            "protocol": payload.get("protocol", ""),
            "dry_run": True,
            "results": [],
            "error_message": f"后端不可用：{exc}",
        }


@st.cache_data(ttl=60, show_spinner=False)
def _protocol_options() -> list[dict]:
    """拉取可用发送协议（含 SMTP 是否已配置），失败时回落默认选项。"""
    try:
        data = httpx.get(f"{BACKEND}/api/email/protocols", timeout=10).json()
        opts = data.get("protocols") or []
        if opts:
            return opts
    except httpx.HTTPError:
        pass
    return [
        {"value": "draft_only", "label": "仅生成草稿（不发送，可复制/导出）"},
        {"value": "mailto", "label": "调用本机邮件客户端发送（mailto 链接）"},
        {"value": "smtp", "label": "由服务端 SMTP 直发（需配置，且每封需显式确认）"},
    ]


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

        # 生成结果：按任务类型分派渲染
        _render_result(idx, turn)


# ---- 结果渲染（按任务类型分派） ----


def _export_block(idx: int, turn: dict, session_id: str, content: str) -> None:
    """导出 + 重试（Q-02：仅由用户点击触发）。"""
    c1, c2 = st.columns(2)
    if c1.button("导出 .md", key=f"export_{idx}", type="primary"):
        turn["export_result"] = _export(session_id, content)
        st.rerun()
    prev = turn["export_result"]
    if c2.button(
        "重试导出",
        key=f"retry_{idx}",
        disabled=not (prev and not prev[0]),
    ):
        turn["export_result"] = _export(session_id, content)
        st.rerun()
    if turn["export_result"] is not None:
        ok, info = turn["export_result"]
        if ok:
            st.success(f"导出成功：{info}")
        else:
            st.error(f"{info}，内容已保留，可点击「重试导出」")


def _derived_block(idx: int, response: dict) -> None:
    """派生建议按钮（理解 C）：只给建议，点击才执行。"""
    tasks = response.get("derived_tasks") or []
    if not tasks:
        return
    st.markdown("**可以继续生成 →**")
    cols = st.columns(len(tasks))
    for col, task in zip(cols, tasks):
        if col.button(
            task["label"],
            key=f"derive_{idx}_{task['task_type']}",
            help=task.get("reason", ""),
            use_container_width=True,
        ):
            _derive(response["session_id"], task["task_type"], task["label"])
            st.rerun()
    for task in tasks:
        if task.get("reason"):
            st.caption(f"· {task['label']}：{task['reason']}")


def _render_minutes(idx: int, turn: dict, response: dict) -> None:
    """会议纪要：可编辑 Markdown + 导出。"""
    markdown = response.get("markdown", "")
    st.markdown("**生成结果**（可编辑，导出以此为准）")
    turn["edited"] = st.text_area(
        "生成结果",
        value=turn.get("edited", markdown),
        height=320,
        label_visibility="collapsed",
        key=f"edit_{idx}",
    )
    _export_block(idx, turn, response["session_id"], turn["edited"])


def _render_weekly(idx: int, turn: dict, response: dict) -> None:
    """周报：结构化展示（进展 / 计划 / 风险）+ 可编辑 Markdown + 导出。"""
    report = response["weekly_report"]
    with st.container(border=True):
        st.markdown(f"**周报** · 周期 {report['period']}")
        st.markdown("**本周进展**")
        if report["progress"]:
            for p in report["progress"]:
                st.markdown(f"- {p['content']}")
        else:
            st.markdown("- 待确认")
        st.markdown("**下周计划**")
        if report["next_plan"]:
            st.dataframe(
                [
                    {
                        "事项": n["content"],
                        "负责人": n["owner"],
                        "计划完成": n["deadline"],
                        "原文依据": n["evidence"],
                    }
                    for n in report["next_plan"]
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.markdown("- 待确认")
        if report["risks"]:
            st.markdown("**风险与待确认**")
            for r in report["risks"]:
                st.warning(r, icon="⚠️")

    st.markdown("**可编辑内容**（导出以此为准）")
    turn["edited"] = st.text_area(
        "周报内容",
        value=turn.get("edited", response.get("markdown", "")),
        height=280,
        label_visibility="collapsed",
        key=f"weekly_edit_{idx}",
    )
    _export_block(idx, turn, response["session_id"], turn["edited"])


def _render_web_summary(idx: int, turn: dict, response: dict) -> None:
    """网页整理（FR-04）：单页要点 + 可编辑 Markdown + 导出。

    约束：单页读取（一次一个 URL），不批量爬取、不登录；
    要点摘自原文段落，不生成评论性内容。
    """
    summary = response["web_summary"]
    with st.container(border=True):
        st.markdown(f"**网页整理** · {summary.get('title') or '待确认'}")
        st.caption(
            f"来源：{summary.get('url', '')}　正文约 {summary.get('word_count', 0)} 字"
        )
        points = summary.get("points") or []
        if points:
            for i, point in enumerate(points, start=1):
                st.markdown(f"{i}. {point}")
        else:
            st.markdown("- 待确认")
        st.caption("要点摘自网页原文段落；单页读取，不支持批量爬取与需登录页面。")

    st.markdown("**可编辑内容**（导出以此为准）")
    turn["edited"] = st.text_area(
        "网页整理内容",
        value=turn.get("edited", response.get("markdown", "")),
        height=280,
        label_visibility="collapsed",
        key=f"web_edit_{idx}",
    )
    _export_block(idx, turn, response["session_id"], turn["edited"])


def _draft_markdown(draft: dict, values: list[dict], protocol: str) -> str:
    """按用户当前编辑内容生成导出用 Markdown（与界面所见一致）。"""
    label = next(
        (o["label"] for o in _protocol_options() if o["value"] == protocol), protocol
    )
    lines = [
        "# 邮件草稿（未发送）",
        "",
        f"- **发件人（本人）**：{draft.get('sender', '待确认')}",
        f"- **发送协议**：{label}",
        f"- **来源会议**：{draft.get('source_topic', '')}（{draft.get('source_date', '')}）",
        f"- **收件人**：{len(values)} 位",
        "",
        f"**主题**：{draft.get('subject', '')}",
        "",
        "---",
        "",
    ]
    for i, r in enumerate(values, start=1):
        lines.append(f"## {i}. {r['name']} <{r['email'] or '（待填写）'}>")
        lines.append("")
        lines.append(f"- 邮箱：{r['email'] or '（待填写）'}")
        lines.append(f"- 发送：{'是' if r['send'] else '否'}")
        lines.append("")
        lines.append("```text")
        lines.append(r["content"] or "（内容为空）")
        lines.append("```")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "> 本文件仅为草稿。邮箱地址由本人手动填写；是否发送逐封确认，工具不会自动发送。"
    )
    return "\n".join(lines)


def _render_email_draft(idx: int, turn: dict, response: dict) -> None:
    """邮件草稿编辑器。

    UI 规格（用户需求）：显示本人 → 协议 → 分析出的人数 → 名字/邮箱/内容/发送。
    """
    draft = response["email_draft"]
    recipients = draft.get("recipients", [])

    # ---- 本人 + 协议 ----
    with st.container(border=True):
        c1, c2 = st.columns([1, 2])
        sender = c1.text_input(
            "发件人（本人）",
            value=draft.get("sender", "待确认"),
            key=f"sender_{idx}",
            help="默认「待确认」；请填写你自己的邮箱或显示名",
        )
        options = _protocol_options()
        values = [o["value"] for o in options]
        default_idx = values.index(draft.get("protocol", "draft_only")) if draft.get(
            "protocol", "draft_only"
        ) in values else 0
        protocol = c2.selectbox(
            "发送协议",
            options=values,
            index=default_idx,
            format_func=lambda v: next(
                (o["label"] for o in options if o["value"] == v), v
            ),
            key=f"protocol_{idx}",
            help="默认仅生成草稿。任何协议下，发送都必须由你点击触发，系统不会自动发送。",
        )
        if protocol == "draft_only":
            st.caption("当前为草稿模式：不会发送任何邮件，可复制内容或导出 .md 自行发送。")
        elif protocol == "mailto":
            st.caption("将生成 mailto 链接，由你本机邮件客户端完成发送。")
        else:
            st.caption("由服务端 SMTP 直发；需在 .env 配置并显式开启，且每封需确认。")

    # ---- 主题 ----
    subject = st.text_input(
        "邮件主题",
        value=draft.get("subject", ""),
        key=f"subject_{idx}",
    )

    # ---- 收件人（人数来自会议纪要分析） ----
    st.markdown(f"**收件人：共从会议纪要中分析出 {len(recipients)} 位**")
    if not recipients:
        st.warning(
            "未能从会议纪要中分析出负责人，无法生成收件人。"
            "请在纪要中补充负责人后重新派生。",
            icon="⚠️",
        )
        return

    st.caption("姓名来自会议纪要；**邮箱需你手动填写**（系统不会推测或拼接邮箱地址）。")

    values_out: list[dict] = []
    for i, r in enumerate(recipients):
        with st.container(border=True):
            head, send_col = st.columns([3, 1])
            head.markdown(f"**{i + 1}. {r['name']}**")
            send = send_col.checkbox(
                "发送这封",
                value=bool(r.get("send", False)),
                key=f"send_{idx}_{i}",
                help="默认不勾选。只有勾选的收件人才会在你点击下方按钮时被处理。",
            )
            email = st.text_input(
                "邮箱",
                value=r.get("email", ""),
                key=f"mail_{idx}_{i}",
                placeholder="you@example.com（需手动填写）",
            )
            content = st.text_area(
                "内容（可编辑）",
                value=r.get("content", ""),
                key=f"body_{idx}_{i}",
                height=150,
            )
            if email and "@" not in email:
                st.caption("⚠️ 邮箱格式看起来不正确")
            values_out.append(
                {"name": r["name"], "email": email, "content": content, "send": send}
            )

    selected = sum(1 for v in values_out if v["send"])
    st.caption(f"已勾选 {selected} / {len(values_out)} 位。未勾选的不会被处理。")

    # ---- 操作按钮 ----
    c1, c2 = st.columns([1, 1])
    action_label = "预览（不发送）" if protocol == "draft_only" else "按协议处理"
    if c1.button(action_label, key=f"emailsend_{idx}", type="primary"):
        turn["send_result"] = _email_send(
            {
                "session_id": response["session_id"],
                "protocol": protocol,
                "sender": sender,
                "subject": subject,
                "recipients": values_out,
                "confirm": protocol != "draft_only",
            }
        )
        st.rerun()

    if c2.button("导出草稿 .md", key=f"export_{idx}"):
        turn["export_result"] = _export(
            response["session_id"],
            _draft_markdown(draft, values_out, protocol),
            filename=f"email_draft_{idx}",
        )
        st.rerun()

    _render_send_result(turn, protocol)
    if turn["export_result"] is not None:
        ok, info = turn["export_result"]
        if ok:
            st.success(f"草稿已导出:{info}")
        else:
            st.error(f"{info}，内容已保留，可重试")


def _render_send_result(turn: dict, protocol: str) -> None:
    """展示发送/预览结果：逐封状态 + dry_run 明确提示。"""
    result = turn.get("send_result")
    if not result:
        return
    if result.get("dry_run", True):
        st.info(
            "**未发送任何邮件**（预览/草稿模式）。"
            + (f" {result.get('error_message')}" if result.get("error_message") else "")
        )
    elif result.get("error_message"):
        st.warning(result["error_message"])
    else:
        st.success("已按协议处理完毕，状态见下表。")
    rows = result.get("results") or []
    if rows:
        st.dataframe(
            [
                {
                    "姓名": r["name"],
                    "邮箱": r["email"] or "（未填写）",
                    "状态": {
                        "sent": "已发送",
                        "preview": "仅预览",
                        "skipped": "已跳过",
                        "failed": "失败",
                    }.get(r["status"], r["status"]),
                    "说明": r["detail"],
                }
                for r in rows
            ],
            use_container_width=True,
            hide_index=True,
        )


def _render_result(idx: int, turn: dict) -> None:
    """按任务类型分派结果渲染，并追加派生建议。"""
    response = turn["response"]
    if (response.get("error_message") or response.get("needs_clarification")) and not (
        response.get("markdown")
        or response.get("email_draft")
        or response.get("web_summary")
    ):
        return

    if response.get("email_draft"):
        _render_email_draft(idx, turn, response)
    elif response.get("weekly_report"):
        _render_weekly(idx, turn, response)
    elif response.get("web_summary"):
        _render_web_summary(idx, turn, response)
    elif response.get("structured_result"):
        _render_minutes(idx, turn, response)
    elif response.get("markdown"):
        st.info(response["markdown"])

    # 派生建议：仅在有产物且原任务允许派生时出现
    _derived_block(idx, response)


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


# ---- 配置面板 ----
#
# 位置：页面内容区右上角（Streamlit 工具栏 Deploy 按钮正下方）。
# 目标：让用户不碰命令行就能选模型、填 Key，并尽量立即生效。


def _fetch_config() -> dict | None:
    """读取后端配置（密钥只有掩码）。"""
    try:
        resp = httpx.get(f"{BACKEND}/api/config", timeout=15)
        if resp.status_code == 200:
            return resp.json()
        return {"error": f"后端返回 HTTP {resp.status_code}"}
    except httpx.HTTPError as exc:
        return {"error": f"读不到配置：后端未启动或不可达（{exc}）"}


def _post_config(updates: dict, clear: list) -> dict:
    """保存配置到后端（写入 .env）。"""
    try:
        resp = httpx.post(
            f"{BACKEND}/api/config",
            json={"updates": updates, "clear": clear},
            timeout=30,
        )
        if resp.status_code == 200:
            return resp.json()
        detail = resp.json().get("detail", resp.text)
        return {"error": str(detail)}
    except httpx.HTTPError as exc:
        return {"error": f"保存失败：{exc}"}


def _post_config_test(base_url: str, api_key: str, model_name: str) -> dict:
    """测试模型连通性（用当前表单里的值，不必先保存）。"""
    try:
        resp = httpx.post(
            f"{BACKEND}/api/config/test",
            json={
                "base_url": base_url or None,
                "api_key": api_key or None,
                "model_name": model_name or None,
            },
            timeout=40,
        )
        if resp.status_code == 200:
            return resp.json()
        return {"ok": False, "detail": f"后端返回 HTTP {resp.status_code}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"后端不可用：{exc}"}


def _cfg_float(value: str, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _cfg_str(items: dict, key: str, fallback: str = "") -> str:
    """取配置项字符串值；未设置时回落到该字段的文档默认值。"""
    item = items.get(key) or {}
    value = item.get("value")
    if value is None or value == "":
        value = item.get("default")
    return str(value) if value not in (None, "") else fallback


def _cfg_bool(items: dict, key: str) -> bool:
    """取布尔配置值。

    注意：必须回落到字段默认值——否则 `.env` 里没写的布尔项会显示为"未勾选"，
    一保存就把默认值（如 SMTP_TLS 的 1）静默改成 0。
    """
    return _cfg_str(items, key, "0").strip() == "1"


def _close_config() -> None:
    """关闭配置面板（主动点「关闭」或点右上角 ✕ 都会走到这里）。"""
    ss["config_open"] = False


@st.dialog("⚙️ 配置", width="large", on_dismiss=_close_config)
def _config_dialog() -> None:
    """配置弹窗：模型与 API Key（核心）+ 抽取引擎 + 邮件 + 文件路径。"""
    cfg = _fetch_config()
    if cfg is None or cfg.get("error"):
        st.error((cfg or {}).get("error", "读不到配置"))
        st.caption("请确认后端已启动：`.\\scripts\\start.ps1 status`")
        return

    items = {it["key"]: it for it in cfg.get("items", [])}
    presets = cfg.get("presets") or []
    preset_ids = [p["id"] for p in presets]
    preset_labels = {p["id"]: p["label"] for p in presets}

    # ---------- 1. 模型服务（核心）----------
    st.markdown("### 🤖 模型服务")
    if cfg.get("model_ready"):
        st.success(
            f"当前已配置：**{items.get('OFFICEFLOW_MODEL_NAME', {}).get('value', '')}**"
            f" @ {items.get('OFFICEFLOW_MODEL_BASE_URL', {}).get('value', '')}",
            icon="✅",
        )
    else:
        st.warning("尚未配置 API Key，当前为**内置规则引擎**（离线可用，但不理解复杂表述）", icon="⚠️")

    cur_preset = cfg.get("preset", "custom")
    idx = preset_ids.index(cur_preset) if cur_preset in preset_ids else max(len(preset_ids) - 1, 0)
    sel = st.selectbox(
        "服务商",
        options=preset_ids,
        index=idx,
        format_func=lambda v: preset_labels.get(v, v),
        key="cfg_provider",
    )
    preset = next((p for p in presets if p["id"] == sel), {"base_url": "", "models": [], "key_hint": ""})

    # 切换服务商时，自动带出该服务商的 Base URL 与默认模型
    if ss.get("cfg_last_provider") != sel:
        ss["cfg_last_provider"] = sel
        if preset.get("base_url"):
            ss["cfg_base_url"] = preset["base_url"]
        if preset.get("models"):
            ss["cfg_model_pick"] = preset["models"][0]

    if "cfg_base_url" not in ss:
        ss["cfg_base_url"] = items.get("OFFICEFLOW_MODEL_BASE_URL", {}).get("value") or preset.get("base_url", "")
    base_url = st.text_input("Base URL", key="cfg_base_url", help="OpenAI 兼容端点，通常以 /v1 结尾")

    models = list(preset.get("models") or [])
    if models:
        if "cfg_model_pick" not in ss:
            cur_model = items.get("OFFICEFLOW_MODEL_NAME", {}).get("value") or models[0]
            ss["cfg_model_pick"] = cur_model if cur_model in models else models[0]
        options = models + ["自定义…"]
        picked = st.selectbox("模型名", options=options, key="cfg_model_pick")
        if picked == "自定义…":
            model_name = st.text_input(
                "自定义模型名", key="cfg_model_custom",
                value=items.get("OFFICEFLOW_MODEL_NAME", {}).get("value", ""),
            )
        else:
            model_name = picked
    else:
        model_name = st.text_input(
            "模型名", key="cfg_model_custom",
            value=items.get("OFFICEFLOW_MODEL_NAME", {}).get("value", ""),
        )

    key_item = items.get("OFFICEFLOW_MODEL_API_KEY", {})
    if key_item.get("configured"):
        st.caption(f"API Key：已配置（{key_item.get('value', '')}）—— 留空表示不修改")
    else:
        st.caption("API Key：**未配置**")
    api_key_input = st.text_input(
        "API Key", type="password", key="cfg_api_key",
        placeholder="粘贴形如 sk-… 的 Key；留空表示不修改已保存的值",
        help=preset.get("key_hint", ""),
    )
    if preset.get("key_hint"):
        st.caption(f"💡 {preset['key_hint']}")
    clear_key = st.checkbox("清除已保存的 API Key", key="cfg_clear_key")

    timeout = st.text_input(
        "超时（秒）", key="cfg_timeout",
        value=items.get("OFFICEFLOW_MODEL_TIMEOUT", {}).get("value", "60"),
    )

    c_test, c_test_msg = st.columns([1, 2])
    if c_test.button("🔌 测试连接", key="cfg_test", use_container_width=True):
        with st.spinner("正在请求模型服务…"):
            ss["cfg_test_result"] = _post_config_test(base_url, api_key_input, model_name)
    res = ss.get("cfg_test_result")
    if res:
        with c_test_msg:
            if res.get("ok"):
                st.success(res.get("detail", "连接成功"))
            else:
                st.error(f"{res.get('detail', '失败')}" + (f"｜{res['hint']}" if res.get("hint") else ""))
        if not res.get("ok") and res.get("error"):
            st.caption(f"原始返回：`{res['error']}`")

    # ---------- 2. 抽取引擎 ----------
    with st.expander("🧩 抽取引擎（进阶）", expanded=False):
        st.caption(
            "默认使用上面配置的 LLM 抽取。开启 LangExtract 后，抽取结果会带**字符级原文坐标**，"
            "无法在原文定位的结果会被直接丢弃 —— 对「零编造」是更强的保证，但会慢一些。"
        )
        lx_on = st.checkbox(
            "启用 LangExtract 溯源抽取",
            value=_cfg_bool(items, "OFFICEFLOW_LANGEXTRACT_ENABLE"),
            key="cfg_langextract",
        )

    # ---------- 3. 邮件发送 ----------
    with st.expander("📧 邮件发送（进阶）", expanded=False):
        st.caption(
            "**默认只生成草稿、绝不发送**（Q-02）。只有打开下面的总开关并填好 SMTP，"
            "且在邮件页逐封确认，才可能真实发送。"
        )
        email_on = st.checkbox(
            "允许真实发送邮件（总开关）",
            value=_cfg_bool(items, "OFFICEFLOW_EMAIL_ENABLE_SEND"),
            key="cfg_email_enable",
            help="保持关闭时，即使选 smtp 协议也只预览，不会发出任何邮件。",
        )
        smtp_cols = st.columns(2)
        smtp_host = smtp_cols[0].text_input(
            "SMTP 服务器", key="cfg_smtp_host",
            value=items.get("OFFICEFLOW_SMTP_HOST", {}).get("value", ""),
        )
        smtp_port = smtp_cols[1].text_input(
            "SMTP 端口", key="cfg_smtp_port",
            value=items.get("OFFICEFLOW_SMTP_PORT", {}).get("value", "587") or "587",
        )
        smtp_user = st.text_input(
            "SMTP 用户名", key="cfg_smtp_user",
            value=items.get("OFFICEFLOW_SMTP_USER", {}).get("value", ""),
        )
        pw_item = items.get("OFFICEFLOW_SMTP_PASSWORD", {})
        st.caption(
            f"SMTP 密码：已配置（{pw_item.get('value', '')}）" if pw_item.get("configured")
            else "SMTP 密码：未配置"
        )
        smtp_password = st.text_input(
            "SMTP 密码 / 授权码", type="password", key="cfg_smtp_password",
            placeholder="留空表示不修改",
        )
        smtp_from = st.text_input(
            "发件人地址", key="cfg_smtp_from",
            value=items.get("OFFICEFLOW_SMTP_FROM", {}).get("value", ""),
        )
        smtp_tls = st.checkbox(
            "使用 STARTTLS（587 端口通常需要）",
            value=_cfg_bool(items, "OFFICEFLOW_SMTP_TLS"),
            key="cfg_smtp_tls",
        )

    # ---------- 4. 应用与文件 ----------
    with st.expander("📁 应用与文件（进阶）", expanded=False):
        backend_url = st.text_input(
            "后端地址", key="cfg_backend",
            value=items.get("OFFICEFLOW_BACKEND", {}).get("value", "http://localhost:8000"),
            help="前端连接的后端地址；改动后需刷新页面生效。",
        )
        export_dir = st.text_input(
            "导出目录", key="cfg_export_dir",
            value=items.get("OFFICEFLOW_EXPORT_DIR", {}).get("value", "exports"),
        )
        db_path = st.text_input(
            "日志数据库路径", key="cfg_db_path",
            value=items.get("OFFICEFLOW_DB_PATH", {}).get("value", "officeflow_logs.db"),
        )
        db_on = st.checkbox(
            "启用日志数据库（验收证据）",
            value=_cfg_bool(items, "OFFICEFLOW_ENABLE_DB"),
            key="cfg_db_enable",
        )
        st.caption("⚠️ 导出目录与数据库路径在**后端启动时**读取，改动后需重启后端才生效。")

    # ---------- 保存 ----------
    st.divider()
    if st.button("💾 保存配置", type="primary", key="cfg_save", use_container_width=True):
        updates: dict[str, str] = {
            "OFFICEFLOW_MODEL_BASE_URL": (base_url or "").strip(),
            "OFFICEFLOW_MODEL_NAME": (model_name or "").strip(),
            "OFFICEFLOW_MODEL_TIMEOUT": (timeout or "").strip(),
            "OFFICEFLOW_LANGEXTRACT_ENABLE": "1" if lx_on else "0",
            "OFFICEFLOW_EMAIL_ENABLE_SEND": "1" if email_on else "0",
            "OFFICEFLOW_SMTP_HOST": (smtp_host or "").strip(),
            "OFFICEFLOW_SMTP_PORT": (smtp_port or "").strip(),
            "OFFICEFLOW_SMTP_USER": (smtp_user or "").strip(),
            "OFFICEFLOW_SMTP_FROM": (smtp_from or "").strip(),
            "OFFICEFLOW_SMTP_TLS": "1" if smtp_tls else "0",
            "OFFICEFLOW_BACKEND": (backend_url or "").strip(),
            "OFFICEFLOW_EXPORT_DIR": (export_dir or "").strip(),
            "OFFICEFLOW_DB_PATH": (db_path or "").strip(),
            "OFFICEFLOW_ENABLE_DB": "1" if db_on else "0",
        }
        if api_key_input.strip():
            updates["OFFICEFLOW_MODEL_API_KEY"] = api_key_input.strip()
        if smtp_password.strip():
            updates["OFFICEFLOW_SMTP_PASSWORD"] = smtp_password.strip()

        clear: list[str] = []
        if clear_key:
            clear.append("OFFICEFLOW_MODEL_API_KEY")

        out = _post_config(updates, clear)
        if out.get("error"):
            st.error(out["error"])
        else:
            applied = out.get("applied") or {}
            st.success(f"已保存 {len(applied)} 项配置到 `.env`")
            if "OFFICEFLOW_MODEL_API_KEY" in applied:
                st.caption("API Key 已更新，**立即生效**（无需重启）。")
            need_restart = out.get("restart_required") or []
            if need_restart:
                st.warning(
                    "以下配置需**重启后端**才生效：" + "、".join(need_restart)
                    + "\n\n```powershell\n.\\scripts\\start.ps1 stop\n.\\scripts\\start.ps1 start\n```",
                    icon="🔄",
                )
            ss["cfg_api_key"] = ""
            ss["cfg_smtp_password"] = ""
            ss.pop("cfg_test_result", None)
            st.caption("提示：改动后建议点一次「🔌 测试连接」确认可用。")

    st.caption(
        "配置文件位置：`" + str(cfg.get("env_file", ".env")) + "`（已在 .gitignore 中，不会被提交）"
    )
    if st.button("关闭", key="cfg_close", use_container_width=True):
        ss["config_open"] = False
        st.rerun()


def _config_topbar() -> None:
    """页面最顶部的右对齐配置入口（视觉上贴着 Streamlit 工具栏的 Deploy 按钮）。

    用 [8, 1] 的空列 + 右对齐列实现，放在任何标题之前；
    打开状态保存在 session_state：面板内交互触发 rerun 后仍保持打开，
    不依赖 dialog 作为 fragment 的隐式行为（AppTest 下尤其明显）。
    """
    _spacer, right = st.columns([8, 1])
    with right:
        if st.button(
            "⚙️",
            key="open_config",
            use_container_width=True,
            help="配置：模型服务与 API Key、抽取引擎、邮件发送、文件路径",
        ):
            ss["config_open"] = True
    if ss.get("config_open"):
        _config_dialog()


# ---- 页面 ----

_config_topbar()

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
                     disabled=not draft.strip(), key="start_button"):
        text = draft.strip()
        ss.draft = ""
        _submit(text)
        st.rerun()  # 切换到对话模式，搜索框沉底
    else:
        # 空状态缺少引导会让人以为页面没内容，这里补一行提示
        st.markdown(
            "<p class='of-sub' style='font-size:0.85rem;margin-top:1rem'>"
            "用法：① 点上方任务按钮（锁定任务类型）→ ② 粘贴原文 → ③ 点「开始处理」"
            "→ ④ 检查结果并「导出 .md」"
            "<br>先用 <b>会议纪要</b> 试：把一段会议记录粘进去即可"
            "</p>",
            unsafe_allow_html=True,
        )
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

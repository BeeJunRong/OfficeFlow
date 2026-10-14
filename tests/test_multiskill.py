"""多 Skill 编排（理解 C）+ 邮件草稿（FR-03）+ 周报整理（FR-02）测试。

覆盖：
- 派生建议只提供有依据的方向；网页整理不参与派生（评估文档 §1）
- 派生复用源产物、不重新抽取；不允许的派生方向被拒绝
- 邮件草稿：收件人来自待办负责人；**邮箱一律留空，绝不编造**；协议默认 draft_only
- 周报：决定→进展、待办→计划，保留 evidence 与"待确认"
- Q-02：生成流程绝不发送；未配置 SMTP 绝不谎报成功
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

N1 = (
    "2026年10月9日项目例会，决定先上线纪要模块。"
    "待办：张三在10月12日前完成解析接口；李四在10月13日前完成前端页面。"
)
# 含无负责人的待办（用于零编造与提示断言）
B1 = (
    "2026年10月9日项目例会，决定先上线纪要模块。"
    "王敏负责调研公开网页解析方案，未约定完成日期。"
)


def _gen(text: str = N1, task_type: str = "meeting_minutes") -> dict:
    resp = client.post("/api/generate", json={"input_text": text, "task_type": task_type})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _derive(session_id: str, task_type: str):
    return client.post(
        "/api/derive", json={"session_id": session_id, "task_type": task_type}
    )


# ---------- 派生建议 ----------


def test_derived_tasks_offered_after_minutes():
    """会议纪要完成后，应给出"邮件草稿 + 周报整理"两项派生建议，且含人数依据。"""
    data = _gen()
    derived = {d["task_type"]: d for d in data["derived_tasks"]}
    assert set(derived) == {"email_draft", "weekly_report"}
    assert "2" in derived["email_draft"]["reason"], "派生理由应包含分析出的人数"
    assert "张三" in derived["email_draft"]["reason"] and "李四" in derived["email_draft"]["reason"]
    assert "1 项决定" in derived["weekly_report"]["reason"]


def test_web_summary_never_derived():
    """网页整理需要 URL/网页文本作输入，会议记录里没有 → 绝不参与派生。"""
    data = _gen()
    assert "web_summary" not in {d["task_type"] for d in data["derived_tasks"]}


def test_email_draft_is_terminal():
    """邮件草稿是终端产物，不再派生（防止 邮件→周报→邮件 循环）。"""
    data = _gen()
    draft = _derive(data["session_id"], "email_draft").json()
    assert draft["derived_tasks"] == []


# ---------- 派生执行 ----------


def test_derive_email_draft_from_minutes():
    """派生邮件草稿：收件人 = 待办负责人去重；邮箱留空；协议默认 draft_only。"""
    data = _gen()
    resp = _derive(data["session_id"], "email_draft")
    assert resp.status_code == 200, resp.text
    out = resp.json()

    assert out["task_type"] == "email_draft"
    assert out["derived_from"] == data["session_id"]
    draft = out["email_draft"]
    assert [r["name"] for r in draft["recipients"]] == ["张三", "李四"]

    for r in draft["recipients"]:
        assert r["email"] == "", "邮箱必须留空由用户手填，系统不得编造"
        assert r["send"] is False, "发送开关默认关闭（Q-02）"
        assert r["content"], "内容应由该负责人的待办生成"

    assert draft["protocol"] == "draft_only"
    assert "解析接口" in draft["recipients"][0]["content"]
    assert "2026-10-12" in draft["recipients"][0]["content"]
    assert "未发送" in out["markdown"]


def test_derive_reuses_source_result():
    """派生复用源产物（工具日志说明"复用…派生"），不重新抽取。"""
    data = _gen()
    out = _derive(data["session_id"], "email_draft").json()
    details = [log["error_message"] for log in out["tool_logs"]]
    assert any("复用" in d for d in details), f"应走复用分支，实际日志：{details}"


def test_derive_weekly_report_from_minutes():
    """周报：关键决定→本周进展；待办→下周计划；周期由会议日期推算自然周。"""
    data = _gen()
    resp = _derive(data["session_id"], "weekly_report")
    assert resp.status_code == 200, resp.text
    out = resp.json()

    report = out["weekly_report"]
    assert [p["content"] for p in report["progress"]] == ["先上线纪要模块"]
    assert [n["content"] for n in report["next_plan"]] == ["解析接口", "前端页面"]
    assert report["next_plan"][0]["owner"] == "张三"
    assert report["next_plan"][0]["deadline"] == "2026-10-12"
    # 2026-10-09 是周五 → 所在自然周 2026-10-05(周一) ~ 2026-10-11(周日)
    assert report["period"] == "2026-10-05 ~ 2026-10-11"


def test_weekly_report_flags_pending_as_risk():
    """负责人/截止日期为"待确认"的条目要进风险清单，不得静默。"""
    data = _gen(B1)
    out = _derive(data["session_id"], "weekly_report").json()
    report = out["weekly_report"]
    assert any("待确认" in r or "未提供" in r for r in report["risks"])
    assert all(n["deadline"] == "待确认" for n in report["next_plan"])


def test_derive_email_draft_from_weekly_report():
    """周报 → 邮件草稿：收件人取下周计划负责人。"""
    data = _gen()
    report = _derive(data["session_id"], "weekly_report").json()
    out = _derive(report["session_id"], "email_draft").json()
    assert out["derived_from"] == report["session_id"]
    assert [r["name"] for r in out["email_draft"]["recipients"]] == ["张三", "李四"]


def test_derive_rejects_unsupported_direction():
    """不允许的派生方向被拒绝（如 邮件草稿 → 周报整理）。"""
    data = _gen()
    draft = _derive(data["session_id"], "email_draft").json()
    resp = _derive(draft["session_id"], "weekly_report")
    assert resp.status_code == 400
    assert "不支持" in resp.json()["detail"]


def test_derive_rejects_web_summary_target():
    """明确拒绝派生出网页整理。"""
    data = _gen()
    resp = _derive(data["session_id"], "web_summary")
    assert resp.status_code == 400


def test_derive_unknown_session_404():
    resp = _derive("nonexist1234", "email_draft")
    assert resp.status_code == 404


# ---------- 发送（Q-02） ----------


def test_generate_never_sends_email():
    """Q-02：生成流程绝不产生发送行为（工具日志中不得出现 email_send）。"""
    data = _gen()
    draft = _derive(data["session_id"], "email_draft").json()
    names = [log["tool_name"] for log in draft["tool_logs"]]
    assert "email_send" not in names
    assert draft["email_draft"]["recipients"][0]["send"] is False


def test_email_send_draft_only_is_preview():
    """默认协议 draft_only：只预览，dry_run=True，收回 status=preview。"""
    draft = _derive(_gen()["session_id"], "email_draft").json()["email_draft"]
    for r in draft["recipients"]:
        r["send"] = True
    resp = client.post(
        "/api/email/send",
        json={
            "protocol": "draft_only",
            "subject": draft["subject"],
            "recipients": draft["recipients"],
            "confirm": True,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True
    assert all(r["status"] == "preview" for r in body["results"])


def test_email_send_requires_selection():
    """未勾选任何收件人 → 不发送，明确报错。"""
    draft = _derive(_gen()["session_id"], "email_draft").json()["email_draft"]
    resp = client.post(
        "/api/email/send",
        json={"protocol": "draft_only", "recipients": draft["recipients"]},
    )
    body = resp.json()
    assert body["dry_run"] is True
    assert "没有勾选" in body["error_message"]


def test_email_send_smtp_unconfigured_never_claims_sent():
    """smtp 未配置时绝不谎报成功（dry_run=True 且状态非 sent）。"""
    draft = _derive(_gen()["session_id"], "email_draft").json()["email_draft"]
    for r in draft["recipients"]:
        r["send"] = True
    resp = client.post(
        "/api/email/send",
        json={
            "protocol": "smtp",
            "subject": draft["subject"],
            "recipients": draft["recipients"],
            "confirm": True,
        },
    )
    body = resp.json()
    assert body["dry_run"] is True
    assert all(r["status"] != "sent" for r in body["results"])
    assert "SMTP" in body["error_message"] or "未配置" in body["error_message"]


def test_email_send_rejects_unknown_protocol():
    resp = client.post(
        "/api/email/send",
        json={"protocol": "telepathy", "recipients": [], "confirm": True},
    )
    body = resp.json()
    assert body["dry_run"] is True
    assert "未知发送协议" in body["error_message"]


def test_email_protocols_endpoint():
    """协议清单端点：默认 draft_only，并说明不会自动发送。"""
    body = client.get("/api/email/protocols").json()
    assert body["default"] == "draft_only"
    assert {p["value"] for p in body["protocols"]} == {"draft_only", "mailto", "smtp"}
    assert "显式点击" in body["note"]


# ---------- 零编造 ----------


def test_email_recipients_only_from_verified_owners():
    """负责人原文未提供时，不得生成收件人（不得猜测姓名）。

    用 B2（"10月20日前完成原型设计，负责人待定"）：该待办负责人应为待确认，
    因此收件人列表为空，并在工具日志中明确提示，绝不凭空造出收件人。
    """
    b2 = (
        "2026年10月10日例会。决定先完成需求分析。"
        "待办：10月20日前完成原型设计，负责人待定。"
    )
    data = _gen(b2)
    out = _derive(data["session_id"], "email_draft").json()
    draft = out["email_draft"]
    assert draft["recipients"] == [], "负责人未确认时不得生成任何收件人"
    assert all(r["name"] != "待确认" for r in draft["recipients"])
    details = " ".join(log["error_message"] for log in out["tool_logs"])
    assert "未提供负责人" in details, f"应提示缺少负责人，实际：{details}"
    assert "未能从会议纪要中分析出负责人" in out["markdown"]


def test_email_recipients_skip_pending_but_keep_valid():
    """同时存在已确认与未确认负责人时：只为已确认者生成收件人，并提示缺口。"""
    mixed = (
        "2026年10月9日项目例会。决定先上线纪要模块。"
        "待办：张三在10月12日前完成解析接口；原型设计由负责人待定，10月20日前完成。"
    )
    data = _gen(mixed)
    out = _derive(data["session_id"], "email_draft").json()
    names = [r["name"] for r in out["email_draft"]["recipients"]]
    assert "张三" in names
    assert "待确认" not in names


def test_email_recipients_are_deduplicated():
    """同一负责人有多项待办时只生成一个收件人，内容合并其全部事项。"""
    multi = (
        "2026年10月9日项目例会。决定先上线纪要模块。"
        "待办：张三在10月12日前完成解析接口；张三在10月13日前完成单元测试。"
    )
    data = _gen(multi)
    out = _derive(data["session_id"], "email_draft").json()
    recipients = out["email_draft"]["recipients"]
    assert [r["name"] for r in recipients] == ["张三"]
    assert "解析接口" in recipients[0]["content"]
    assert "单元测试" in recipients[0]["content"]


def test_weekly_report_keeps_evidence():
    """周报计划条目保留原文 evidence，便于逐条核对。"""
    out = _derive(_gen()["session_id"], "weekly_report").json()
    item = out["weekly_report"]["next_plan"][0]
    assert item["evidence"], "应保留原文证据片段"
    assert item["evidence"] in N1, "证据必须是原文子串"

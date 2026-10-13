"""FR-03 邮件草稿测试：字段抽取、零编造、Q-02（只生成草稿，无发送端点）。"""

from fastapi.testclient import TestClient

from app.main import app
from app.schemas import PENDING
from app.skills.email_draft import render_markdown, rule_based_extract

client = TestClient(app)

SAMPLE_EMAIL = (
    "写邮件草稿：收件人：张老师，主题：关于项目进度的汇报，"
    "要点：会议纪要模块已完成开发；下周开始周报模块联调；请老师审阅阶段报告。"
    "落款：马浚荣"
)

SAMPLE_MINIMAL = "帮我写一封邮件草稿：需要跟同学约时间讨论界面设计。"


def test_rule_extract_fields():
    draft = rule_based_extract(SAMPLE_EMAIL)
    assert draft.subject == "关于项目进度的汇报"
    assert draft.recipient == "张老师"
    assert draft.sender == "马浚荣"
    assert any("会议纪要模块" in p for p in draft.key_points)
    assert any("周报模块" in p for p in draft.key_points)


def test_rule_extract_missing_fields_pending():
    draft = rule_based_extract(SAMPLE_MINIMAL)
    assert draft.subject == PENDING
    assert draft.recipient == PENDING
    assert draft.sender == PENDING
    assert any("界面设计" in p for p in draft.key_points)


def test_render_markdown_structure():
    draft = rule_based_extract(SAMPLE_EMAIL)
    md = render_markdown(draft)
    assert "关于项目进度的汇报" in md
    assert "张老师" in md
    assert "马浚荣" in md
    # 草稿声明：不自动发送（Q-02 的用户可见承诺）
    assert "不会自动发送" in md


def test_generate_api_email_draft():
    resp = client.post(
        "/api/generate",
        json={"input_text": SAMPLE_EMAIL, "task_type": "email_draft"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "email_draft"
    result = data["structured_result"]
    assert result["subject"] == "关于项目进度的汇报"
    assert result["recipient"] == "张老师"
    assert "不会自动发送" in data["markdown"]


def test_q02_no_send_endpoint_anywhere():
    """Q-02（100%）：整个 API 不存在任何发送邮件的端点。"""
    paths = {route.path for route in app.routes}
    for forbidden in ("send", "smtp", "mail/send"):
        assert not any(forbidden in p.lower() for p in paths), f"发现疑似发送端点: {paths}"


def test_email_zero_fabrication_on_fields():
    """LLM 路径编造字段的回退在 API 层验证：规则引擎抽取 + 校验。"""
    # 规则引擎只取原文出现的字段值，天然满足；此处验证空字段为待确认
    gen = client.post(
        "/api/generate",
        json={"input_text": SAMPLE_MINIMAL, "task_type": "email_draft"},
    ).json()
    result = gen["structured_result"]
    for field in ("subject", "recipient", "sender"):
        assert result[field] == "待确认"

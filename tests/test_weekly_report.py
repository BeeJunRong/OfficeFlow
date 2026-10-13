"""FR-02 周报整理测试：分节解析、无分节语义分类、周期零编造、导出。"""

from fastapi.testclient import TestClient

from app.main import app
from app.skills.weekly_report import rule_based_extract, render_markdown

client = TestClient(app)

SAMPLE_SECTIONED = (
    "周报（10月9日至10月13日）：\n"
    "本周工作：完成解析接口开发；修复导出重试的重复记录问题。\n"
    "下周计划：启动前端联调；编写验收文档。\n"
    "风险：LLM API 免费额度可能不足。"
)

SAMPLE_PLAIN = (
    "本周完成了会议纪要模块的开发，导出功能已上线。"
    "下周计划继续优化路由准确率。"
    "存在风险：样本冻结进度延迟。"
)


def test_rule_extract_sectioned():
    report = rule_based_extract(SAMPLE_SECTIONED)
    assert report.period == "10月9日 ~ 10月13日"
    contents_done = [w.content for w in report.work_done]
    assert any("解析接口" in c for c in contents_done)
    assert any("重复记录" in c for c in contents_done)
    assert all(w.status == "已完成" for w in report.work_done)
    contents_plan = [p.content for p in report.plans]
    assert any("前端联调" in c for c in contents_plan)
    assert any("验收文档" in c for c in contents_plan)
    assert any("额度" in r for r in report.risks)


def test_rule_extract_plain_classification():
    report = rule_based_extract(SAMPLE_PLAIN)
    assert any("会议纪要模块" in w.content for w in report.work_done)
    assert any("路由准确率" in p.content for p in report.plans)
    assert any("样本冻结" in r for r in report.risks)
    # 周期原文无明确区间 → 待确认（零编造）
    assert report.period == "待确认"


def test_period_zero_fabrication():
    report = rule_based_extract("本周做了一些事情，下周继续。")
    assert report.period == "待确认"


def test_render_markdown_sections():
    report = rule_based_extract(SAMPLE_SECTIONED)
    md = render_markdown(report)
    for header in ("# 周报", "## 本周工作", "## 下周计划", "## 风险与问题"):
        assert header in md
    assert "10月9日 ~ 10月13日" in md


def test_generate_api_weekly():
    resp = client.post(
        "/api/generate",
        json={"input_text": SAMPLE_SECTIONED, "task_type": "weekly_report"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "weekly_report"
    assert data["routed_by"] == "explicit"
    result = data["structured_result"]
    assert result["period"] == "10月9日 ~ 10月13日"
    assert len(result["work_done"]) == 2
    assert len(result["plans"]) == 2
    # 工具链路完整：route → extract → validate → render
    tools = [log["tool_name"] for log in data["tool_logs"]]
    assert tools[0] == "route"
    assert "extract_fields" in tools and "render_markdown" in tools
    assert data["markdown"]


def test_weekly_empty_input_blocked():
    resp = client.post(
        "/api/generate", json={"input_text": "  ", "task_type": "weekly_report"}
    )
    assert resp.status_code == 400


def test_weekly_export():
    gen = client.post(
        "/api/generate",
        json={"input_text": SAMPLE_SECTIONED, "task_type": "weekly_report"},
    ).json()
    exp = client.post(
        "/api/export",
        json={"session_id": gen["session_id"], "content": gen["markdown"], "filename": "w1"},
    ).json()
    assert exp["success"] is True
    from pathlib import Path

    assert "下周计划" in Path(exp["file_path"]).read_text(encoding="utf-8")

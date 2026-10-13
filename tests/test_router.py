"""FR-00 Skill 路由测试：T4 路由边界 + Q-03 路由准确性（guardsrails §3）。

- 显式点击优先且锁定（100%）
- 指令区关键词快速匹配
- 正文关键词不触发路由（T4 核心）
- LLM 不可用时降级追问（needs_clarification），不静默路由
"""

from fastapi.testclient import TestClient

from app.main import app
from app.router import route_task, split_instruction
from tests.samples.samples import get_sample

client = TestClient(app)


def test_explicit_click_locks_task_type():
    """显式点击路径：任务类型识别 100%（Q-03）。"""
    sample = get_sample("T4")
    result = route_task(sample["input"], "meeting_minutes")
    assert result.task_type == "meeting_minutes"
    assert result.routed_by == "explicit"
    assert not result.needs_clarification


def test_t4_body_keyword_does_not_misroute():
    """T4：正文含"周报"不误路由，任务类型保持 meeting_minutes。"""
    sample = get_sample("T4")
    resp = client.post(
        "/api/generate",
        json={"input_text": sample["input"], "task_type": "meeting_minutes"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_type"] == "meeting_minutes", "正文'周报'导致误路由"
    assert data["routed_by"] == "explicit"
    # 输出仍符合 FR-01
    result = data["structured_result"]
    assert result["date"] == "2026-10-09"
    assert any("解析接口" in i["task"] for i in result["action_items"])


def test_instruction_keyword_matching():
    """指令区关键词：只看开头/冒号前的短指令。"""
    assert route_task("帮我做会议纪要：2026年10月9日例会……").task_type == "meeting_minutes"
    assert route_task("整理周报：本周完成了……").task_type == "weekly_report"
    assert route_task("写邮件草稿：关于……").task_type == "email_draft"


def test_body_only_keyword_routes_to_clarification():
    """正文关键词不触发：无指令区关键词时走 LLM 兜底；LLM 不可用 → 追问。"""
    # 正文提到"周报/邮件"但指令区无关键词 → 不应静默路由到具体任务
    result = route_task("2026年10月9日项目例会。下周周报由李四负责；邮件模板下周评审。")
    # 测试环境无 LLM Key → needs_clarification（降级方案）
    assert result.needs_clarification is True
    assert result.task_type == ""


def test_split_instruction():
    assert split_instruction("帮我做会议纪要：正文内容") == ("帮我做会议纪要", "正文内容")
    assert split_instruction("帮我做会议纪要：正文：含冒号") == ("帮我做会议纪要", "正文：含冒号")
    instruction, body = split_instruction("没有冒号的短输入")
    assert body == "没有冒号的短输入"


def test_direct_input_meeting_intent_via_keyword():
    """Q-03 直接输入路径：常见会议纪要表述正确路由（≥9/10 中的一部分）。"""
    inputs = [
        "帮我做会议纪要：2026年10月9日项目例会……",
        "请整理会议纪要，内容如下……",
        "会议记录：今天讨论了……",
        "整理一下会议总结：……",
        "帮我做一份会议纪要，以下是记录……",
    ]
    for text in inputs:
        result = route_task(text)
        assert result.task_type == "meeting_minutes", f"误路由: {text} → {result.task_type}"


def test_empty_input_needs_clarification():
    result = route_task("")
    assert result.needs_clarification is True


def test_invalid_explicit_task_type_rejected():
    import pytest

    with pytest.raises(ValueError):
        route_task("任意", "unknown_task")

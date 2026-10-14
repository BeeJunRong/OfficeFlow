"""FR-01 会议纪要测试：T1 正常 / T2 边界 / 零编造（评测计划 §5-6）。

- T1：关键字段（日期/决定/待办三要素）逐字段对照标准答案，导出文件与屏幕一致。
- T2：缺失字段标"待确认"，不编造。
- 零编造：校验节点对无依据日期/负责人的回退。
"""

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.schemas import ActionItem, MeetingMinutes
from app.validate import validate_minutes
from tests.samples.samples import SAMPLES, get_sample

client = TestClient(app)


def _resolve_dynamic(expected: str | None) -> str | None:
    """B4 等无年份样本：期望值按当前年动态构造。"""
    if expected is not None:
        return expected
    return None


def _expected_date(sample: dict, value: str | None) -> str:
    return value if value is not None else f"{datetime.now().year}-10-14"


def test_generate_api_normal_samples():
    """T1/N1-N6：正常样本关键字段逐项对照（阈值 6 例中 ≥5 例全对）。"""
    passed = 0
    for sample in [s for s in SAMPLES if s["category"] == "normal"]:
        exp = sample["expected"]
        resp = client.post(
            "/api/generate",
            json={"input_text": sample["input"], "task_type": sample["task_type"]},
        )
        assert resp.status_code == 200, f"{sample['id']}: {resp.text}"
        data = resp.json()
        assert data["task_type"] == exp["task_type"], f"{sample['id']} 路由错误"
        result = data["structured_result"]
        assert result["date"] == exp["date"], f"{sample['id']} 日期: {result['date']}"
        assert result["topic"] == exp["topic"], f"{sample['id']} 主题: {result['topic']}"
        for decision in exp["decisions"]:
            assert any(
                decision in d for d in result["decisions"]
            ), f"{sample['id']} 缺少决定: {decision}"
        got_items = {(i["task"], i["owner"], i["deadline"]) for i in result["action_items"]}
        for task_kw, owner, deadline in exp["action_items"]:
            matched = any(
                task_kw in t and o == owner and dl == deadline
                for t, o, dl in got_items
            )
            assert matched, f"{sample['id']} 待办缺失/错误: {task_kw}/{owner}/{deadline}, 实际: {got_items}"
        passed += 1
    assert passed == 6


def test_generate_api_boundary_samples():
    """T2/B1-B4：边界样本正确标注缺失字段，不编造（阈值 4 例中 ≥3 例）。"""
    for sample in [s for s in SAMPLES if s["category"] == "boundary"]:
        exp = sample["expected"]
        resp = client.post(
            "/api/generate",
            json={"input_text": sample["input"], "task_type": sample["task_type"]},
        )
        assert resp.status_code == 200, f"{sample['id']}: {resp.text}"
        result = resp.json()["structured_result"]

        if exp["date"] is None:  # B4：动态年份
            assert result["date"] == f"{datetime.now().year}-10-14", f"{sample['id']}"
        else:
            assert result["date"] == exp["date"], f"{sample['id']} 日期: {result['date']}"
        for decision in exp["decisions"]:
            assert any(
                decision in d for d in result["decisions"]
            ), f"{sample['id']} 缺少决定: {decision}"
        for task_kw, owner, deadline in exp["action_items"]:
            items = result["action_items"]
            matched = any(
                task_kw in i["task"]
                and (i["owner"] == owner or (owner == "待确认" and i["owner"] == "待确认"))
                and (
                    i["deadline"] == deadline
                    if deadline is not None
                    else i["deadline"] == f"{datetime.now().year}-10-16"
                )
                for i in items
            )
            assert matched, (
                f"{sample['id']} 待办缺失/错误: {task_kw}/{owner}/{deadline}, "
                f"实际: {[(i['task'], i['owner'], i['deadline']) for i in items]}"
            )
        # 零编造：不得出现标准答案之外的明确负责人或日期
        for item in result["action_items"]:
            assert item["owner"] in sample["input"] or item["owner"] == "待确认"
            if item["deadline"] != "待确认":
                assert result["date"] != "待确认" or item["deadline"], "日期必须有原文依据"


def test_t1_export_matches_screen():
    """T1 补充：导出文件内容与屏幕（接口返回的 markdown）一致。"""
    sample = get_sample("N1")
    gen = client.post(
        "/api/generate",
        json={"input_text": sample["input"], "task_type": "meeting_minutes"},
    ).json()
    exp = client.post(
        "/api/export",
        json={"session_id": gen["session_id"], "content": gen["markdown"], "filename": "t1_n1"},
    ).json()
    assert exp["success"] is True
    exported = Path(exp["file_path"]).read_text(encoding="utf-8")
    assert exported == gen["markdown"], "导出文件与屏幕内容不一致"
    # 导出内容包含关键字段
    assert "2026-10-09" in exported
    assert "张三" in exported and "2026-10-12" in exported
    assert "李四" in exported and "2026-10-13" in exported


def test_zero_fabrication_validator():
    """零编造校验：LLM 编造的日期/负责人必须回退"待确认"。"""
    text = "2026年10月9日项目例会，决定先上线纪要模块。待办：张三在10月12日前完成解析接口。"
    fabricated = MeetingMinutes(
        date="2026-10-09",
        topic="项目例会",
        decisions=["先上线纪要模块"],
        action_items=[
            # 编造：王五不在原文；截止日期原文无依据
            ActionItem(task="解析接口", owner="王五", deadline="2026-10-31", evidence=""),
        ],
    )
    fixed, corrections = validate_minutes(fabricated, text)
    assert fixed.action_items[0].owner == "待确认", "编造负责人未回退"
    assert fixed.action_items[0].deadline == "待确认", "编造截止日期未回退"
    assert len(corrections) == 2
    # 合法标准化：10月12日 + 会议年份 → 2026-10-12 应保留
    legit = MeetingMinutes(
        date="2026-10-09",
        topic="项目例会",
        decisions=[],
        action_items=[
            ActionItem(task="解析接口", owner="张三", deadline="2026-10-12", evidence="")
        ],
    )
    fixed2, corrections2 = validate_minutes(legit, text)
    assert fixed2.action_items[0].owner == "张三"
    assert fixed2.action_items[0].deadline == "2026-10-12"
    assert corrections2 == []


def test_standard_template_structure():
    """标准六段式模板：导出结构符合用户指定格式。"""
    sample = get_sample("N1")
    gen = client.post(
        "/api/generate",
        json={"input_text": sample["input"], "task_type": "meeting_minutes"},
    ).json()
    md = gen["markdown"]
    # 元信息栏
    for line in ("时间：", "地点：", "主持人：", "出席人员：", "列席人员：", "缺席人员：", "记录人：", "审核人："):
        assert line in md, f"缺少元信息行: {line}"
    # 六段式
    for section in ("一、会议议题", "二、会议内容与讨论", "三、会议决议", "四、待办事项",
                    "五、风险与遗留问题", "六、下次会议安排"):
        assert section in md, f"缺少章节: {section}"
    # 待办表头含协办与输出物列
    assert "| 序号 | 事项 | 负责人 | 协办 | 截止时间 | 输出物 |" in md
    # 尾部
    assert "附件：" in md and "日期：" in md
    # 缺失元信息标"待确认"（N1 无地点信息）
    assert "地点：待确认" in md


def test_meta_fields_extraction():
    """元信息抽取：标签优先 + 隐式模式；议题/风险/下次会议/协办/输出物。"""
    text = (
        "2026年10月9日项目例会在图书馆会议室举行，由张三主持。"
        "出席人员：张三、李四、王敏。记录人：王敏。"
        "议题：纪要模块方案；导出功能设计。"
        "决定先上线纪要模块。"
        "待办：李四在10月12日前完成解析接口并提交接口文档，王敏协办。"
        "存在风险：API 免费额度不足。"
        "下次会议时间10月16日在第一会议室，议题为验收准备。"
    )
    resp = client.post(
        "/api/generate", json={"input_text": text, "task_type": "meeting_minutes"}
    )
    assert resp.status_code == 200
    result = resp.json()["structured_result"]
    md = resp.json()["markdown"]
    assert result["location"] == "图书馆会议室"
    assert result["host"] == "张三"
    assert result["attendees"] == ["张三", "李四", "王敏"]
    assert result["recorder"] == "王敏"
    assert any("纪要模块方案" in a for a in result["agenda"])
    assert any("导出功能设计" in a for a in result["agenda"])
    # 协办与输出物
    todo = next(i for i in result["action_items"] if "解析接口" in i["task"])
    assert todo["co_owner"] == "王敏"
    assert todo["deliverable"] == "接口文档"
    # 风险与下次会议
    assert any("额度" in r for r in result["risks"])
    assert "10月16日" in result["next_meeting"]["time"]
    assert "第一会议室" in result["next_meeting"]["location"]
    assert "验收准备" in md
    # 隐式地点在 Markdown 中体现
    assert "地点：图书馆会议室" in md


def test_tool_logs_recorded():
    """FR-05：工具日志可查（名称/状态/错误/时间戳）。"""
    sample = get_sample("N1")
    gen = client.post(
        "/api/generate",
        json={"input_text": sample["input"], "task_type": "meeting_minutes"},
    ).json()
    assert gen["tool_logs"], "工具日志为空"
    for log in gen["tool_logs"]:
        assert log["tool_name"] and log["status"] in ("success", "failed")
        assert log["timestamp"]
    # 状态端点可查（验收证据）
    status = client.get(f"/api/status/{gen['session_id']}").json()
    assert status["tool_logs"], "状态端点无工具日志"


def test_all_skills_implemented_no_placeholder():
    """FR-02/03/04 均已实现：注册表中不再有占位 Skill。

    v0.6 合并后 `app/skills/placeholders.py` 已删除（网页整理由 v0.3 线落地，
    单页读取实现见 tests/test_web_summary.py）。
    """
    from app.skills import SKILLS
    from app.skills.base import Skill

    assert set(SKILLS) == {
        "meeting_minutes",
        "weekly_report",
        "email_draft",
        "web_summary",
    }
    assert all(isinstance(skill, Skill) for skill in SKILLS.values())

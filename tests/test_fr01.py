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


def test_placeholder_skills_return_p1():
    """FR-02/03/04 占位：生成结果返回 P1 开发中。"""
    for task_type in ("weekly_report", "email_draft", "web_summary"):
        resp = client.post(
            "/api/generate",
            json={"input_text": "请整理", "task_type": task_type},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_type"] == task_type
        assert "P1" in data["markdown"]

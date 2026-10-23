"""ReAct 通道回归测试（离线假 LLM）。

覆盖真实故障场景：
- LLM 在 function calling 参数里转述 minutes 时把键名写成 content（缺 task）；
- 多步规划"会议纪要 + 周报"；
- LLM 忘记调用 render_* 时的兜底渲染。
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.react_node as react_node
from app.main import app
from app.react_tools import _coerce_action_items, _coerce_weekly_items
from app.schemas import PENDING
from app.tools.export_markdown import render_markdown
from app.tools.extract_fields import rule_based_extract
from app.validate import validate_minutes

client = TestClient(app)

MULTILINE = (
    "2026年10月9日项目例会。\n"
    "决定先上线报表导出优化。\n"
    "待办：阿敏在10月12日前完成数据库连接池调整。\n"
    "小李在10月13日前完成登录页修复。"
)

INPUT = (
    "2026年10月9日项目例会。决定先上线报表导出优化。"
    "待办：阿敏在10月12日前完成数据库连接池调整，"
    "小李在10月13日前完成登录页修复。帮我做会议纪要，接着做一份周报。"
)


class FakeLLM:
    """按脚本依次返回 function calling 响应。"""

    is_available = True

    def __init__(self, script: list[dict[str, Any]]) -> None:
        self.script = script
        self.index = 0
        self.calls: list[list[dict[str, Any]]] = []

    def chat_with_tools(self, messages, tools, temperature=0.1):
        self.calls.append(messages)
        step = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return step


def _call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {"content": "", "tool_calls": [{"id": call_id, "name": name, "arguments": args}]}


def _final(text: str = "完成") -> dict[str, Any]:
    return {"content": text, "tool_calls": []}


def _run(script: list[dict[str, Any]], monkeypatch) -> dict[str, Any]:
    fake = FakeLLM(script)
    monkeypatch.setattr(react_node, "get_llm", lambda: fake)
    return react_node.react_node(
        {"session_id": "t", "input_text": INPUT, "task_type": None, "tool_logs": []}
    )


def test_react_multistep_minutes_and_weekly_with_bad_llm_payload(monkeypatch):
    """回归：LLM 传错 minutes（content 键）时，validate/render 仍基于状态机真实产物。"""
    script = [
        # 抽取（真实规则引擎，因测试环境无 LLM Key）
        _call("1", "extract_fields", {"text": INPUT}),
        # 关键：LLM 转述的 minutes 形状错误（缺 task，键名用 content）
        _call(
            "2",
            "validate",
            {
                "minutes": {
                    "topic": "项目例会",
                    "action_items": [
                        {"content": "找运维调一下", "owner": "阿敏", "deadline": "待确认"}
                    ],
                },
                "source_text": "被 LLM 截断的原文",
            },
        ),
        _call("3", "render_markdown", {}),
        _call("4", "build_weekly_report", {}),
        _call("5", "render_weekly", {}),
        _final(),
    ]
    state = _run(script, monkeypatch)

    # 1) 不再出现反序列化失败
    failed = [log for log in state["tool_logs"] if log["status"] == "failed"]
    assert not failed, f"出现失败工具日志: {failed}"
    validate_logs = [log for log in state["tool_logs"] if log["tool_name"] == "validate"]
    # 循环会先写一条 running，再写一条 success；不得出现 failed
    assert any(log["status"] == "success" for log in validate_logs)
    assert not any(log["status"] == "failed" for log in validate_logs)

    # 2) 两个产物都生成且都有 Markdown
    outputs = state["outputs"]
    types = [o.get("task_type") for o in outputs]
    assert types == ["meeting_minutes", "weekly_report"], types
    assert all(o.get("markdown") for o in outputs)
    assert state["markdown"]

    # 3) 使用抽取器的真实结果（两条待办），不是 LLM 编造的那一条
    items = state["structured_result"]["action_items"]
    assert len(items) == 2
    assert {i["owner"] for i in items} == {"阿敏", "小李"}
    assert all(i["task"] for i in items)

    # 4) 周报内容正确派生
    weekly = state["weekly_report"]
    assert {p["owner"] for p in weekly["next_plan"]} == {"阿敏", "小李"}

    # 5) 路由标识与多步迭代
    assert state["routed_by"] == "react"
    assert state["react_iteration"] >= 5


def test_react_synthesizes_markdown_when_llm_skips_render(monkeypatch):
    """兜底：LLM 只抽取+校验就结束，未调用 render_*，仍自动渲染最终结果。"""
    script = [
        _call("1", "extract_fields", {"text": INPUT}),
        _call("2", "validate", {}),
        _final("已经处理完毕"),
    ]
    state = _run(script, monkeypatch)

    assert state["outputs"], "兜底渲染未产出任何结果"
    assert state["outputs"][0]["task_type"] == "meeting_minutes"
    assert state["outputs"][0]["markdown"]
    assert state["markdown"]
    assert any("兜底自动渲染" in log["error_message"] for log in state["tool_logs"])


def test_react_synthesizes_all_products_when_render_partially_skipped(monkeypatch):
    """兜底：已派生周报但只渲染了纪要时，自动补齐周报产物。"""
    script = [
        _call("1", "extract_fields", {"text": INPUT}),
        _call("2", "validate", {}),
        _call("3", "render_markdown", {}),
        _call("4", "build_weekly_report", {}),
        _final(),  # 忘记 render_weekly
    ]
    state = _run(script, monkeypatch)

    types = [o.get("task_type") for o in state["outputs"]]
    assert types == ["meeting_minutes", "weekly_report"], types
    assert all(o.get("markdown") for o in state["outputs"])


def test_react_validate_uses_cleaned_source_not_llm_args(monkeypatch):
    """零编造校验必须用系统清理后的原文，而不是 LLM 转述的 source_text。"""
    script = [
        _call("1", "extract_fields", {"text": INPUT}),
        _call(
            "2",
            "validate",
            {"minutes": {}, "source_text": "LLM 自己编的原文（不含任何待办）"},
        ),
        _call("3", "render_markdown", {}),
        _final(),
    ]
    state = _run(script, monkeypatch)
    items = state["structured_result"]["action_items"]
    # 若用了 LLM 的 source_text，所有负责人/日期都会被回退为待确认
    assert {i["owner"] for i in items} == {"阿敏", "小李"}
    assert all(i["deadline"] != PENDING for i in items)


def test_coerce_action_items_shapes():
    """容错归一化：content 键、task=None、中文键名、字符串条目。"""
    raw = [
        {"content": "找运维调一下", "deadline": "待确认"},
        {"task": None, "content": "修复登录页", "owner": "小李"},
        {"事项": "调整连接池", "负责人": "阿敏", "截止时间": "2026-10-12"},
        "王五：补充测试报告",
        {"description": "上线报表优化", "assignee": "张三", "due": "2026-10-15"},
    ]
    items = _coerce_action_items(raw)
    assert [i["task"] for i in items] == [
        "找运维调一下",
        "修复登录页",
        "调整连接池",
        "补充测试报告",
        "上线报表优化",
    ]
    assert all(i["task"] for i in items)
    assert items[1]["owner"] == "小李"
    assert items[2]["owner"] == "阿敏"
    assert items[2]["deadline"] == "2026-10-12"
    assert items[3]["owner"] == "王五"


def test_evidence_line_numbers():
    """原文依据带行号：待办证据定位到原始输入的具体行。"""
    minutes = rule_based_extract(MULTILINE)
    fixed, _ = validate_minutes(minutes, MULTILINE, line_source_text=MULTILINE)
    lines = {i.task: i.evidence_line for i in fixed.action_items}
    assert lines["数据库连接池调整"] == 3
    assert lines["登录页修复"] == 4

    markdown = render_markdown(fixed)
    assert "第3行" in markdown and "第4行" in markdown
    assert "数据库连接池调整" in markdown


def test_weekly_evidence_line_numbers_via_api():
    """周报（显式通道）的进展/计划都带原文行号。"""
    resp = client.post(
        "/api/generate", json={"input_text": MULTILINE, "task_type": "weekly_report"}
    )
    assert resp.status_code == 200
    data = resp.json()
    report = data["weekly_report"]
    assert report["next_plan"] and all(p["evidence_line"] for p in report["next_plan"])
    assert any(p["evidence_line"] == 2 for p in report["progress"])
    assert "原文第2行" in data["markdown"]
    assert "第3行" in data["markdown"] and "第4行" in data["markdown"]


def test_cross_turn_seed_weekly_and_email(monkeypatch):
    """跨轮"接着做"：上一轮纪要注入状态，周报计划与邮件收件人不再为空。"""
    # 第 1 轮：正常生成会议纪要（显式通道，离线规则引擎）
    first = client.post(
        "/api/generate", json={"input_text": MULTILINE, "task_type": "meeting_minutes"}
    ).json()
    assert first["structured_result"]["action_items"]
    history = [
        {"role": "user", "content": "帮我做会议纪要", "task_type": "", "has_result": False},
        {
            "role": "assistant",
            "content": "会议纪要已生成",
            "task_type": "meeting_minutes",
            "has_result": True,
            "session_id": first["session_id"],
        },
    ]

    # 第 2 轮：自动识别 + "接着整理成周报"（LLM 只选工具，数据由状态注入）
    monkeypatch.setattr(
        react_node,
        "get_llm",
        lambda: FakeLLM(
            [
                _call("w1", "build_weekly_report", {}),
                _call("w2", "render_weekly", {}),
                _final(),
            ]
        ),
    )
    weekly = client.post(
        "/api/generate",
        json={"input_text": "接着帮我整理成周报", "task_type": None, "history": history},
    ).json()
    report = weekly["weekly_report"]
    assert report is not None
    assert {p["owner"] for p in report["next_plan"]} == {"阿敏", "小李"}
    assert all(p["evidence_line"] for p in report["next_plan"])
    assert weekly["markdown"]

    # 第 3 轮：自动识别 + "帮我发送邮件"（复用第 2 轮周报，而不是空的 LLM 转述）
    history2 = history + [
        {"role": "user", "content": "接着帮我整理成周报", "task_type": "", "has_result": False},
        {
            "role": "assistant",
            "content": "周报已生成",
            "task_type": "weekly_report",
            "has_result": True,
            "session_id": weekly["session_id"],
        },
    ]
    monkeypatch.setattr(
        react_node,
        "get_llm",
        lambda: FakeLLM(
            [
                _call("e1", "build_email_draft", {}),
                _call("e2", "render_email", {}),
                _final(),
            ]
        ),
    )
    email = client.post(
        "/api/generate",
        json={"input_text": "帮我发送邮件", "task_type": None, "history": history2},
    ).json()
    draft = email["email_draft"]
    assert draft is not None
    assert {r["name"] for r in draft["recipients"]} == {"阿敏", "小李"}
    assert all(not r["email"] for r in draft["recipients"])  # 邮箱留空待手填
    assert email["markdown"]


def test_coerce_weekly_items_shapes():
    items = _coerce_weekly_items(
        [
            {"item": "完成登录页", "owner": "小李"},
            {"content": "调整连接池", "负责人": "阿敏", "截止日期": "2026-10-12"},
            "补充测试",
        ]
    )
    assert [i["content"] for i in items] == ["完成登录页", "调整连接池", "补充测试"]
    assert items[1]["owner"] == "阿敏"
    assert items[1]["deadline"] == "2026-10-12"

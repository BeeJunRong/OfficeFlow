"""13 例冻结合成样本与标准答案（评测计划 §4）。

- 正常 6（N1-N6）、差异/边界 4（B1-B4）、异常 2（E1-E2）、路由边界 1（T4）。
- N1 / B1 / E1 / T4 输入来自 docs/评测计划.md §4.1 原文；其余由张敏杰角色
  在测试前冻结补充（本文件即冻结版本，标准答案为手工编写，非 AI 输出）。
- expected.action_items 的 task 断言采用"包含"语义（清洗后的短语保留核心词），
  owner/deadline 为精确断言（owner 允许包含在原文中即可核对）。

样本 schema：
{
  "id": "N1",
  "category": "normal|boundary|exception|route",
  "task_type": "meeting_minutes",        # 显式点击锁定的任务类型（None=直接输入）
  "input": "...",                        # 会议原始记录
  "expected": {
      "date": "2026-10-09",              # "待确认" 表示应标缺失
      "topic": "项目例会",
      "decisions": ["先上线纪要模块"],   # 子串匹配断言
      "action_items": [                  # (task关键词, owner, deadline)
          ("解析接口", "张三", "2026-10-12"),
      ],
      "task_type": "meeting_minutes",    # 路由断言（T4）
  },
}
"""

from typing import Any

SAMPLES: list[dict[str, Any]] = [
    # ---------- 正常 N1-N6 ----------
    {
        "id": "N1",
        "category": "normal",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月9日项目例会，决定先上线纪要模块。"
            "待办：张三在10月12日前完成解析接口；李四在10月13日前完成前端页面。"
        ),
        "expected": {
            "date": "2026-10-09",
            "topic": "项目例会",
            "decisions": ["先上线纪要模块"],
            "action_items": [
                ("解析接口", "张三", "2026-10-12"),
                ("前端页面", "李四", "2026-10-13"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "N2",
        "category": "normal",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月12日小组周会。决定下周三进行中期检查演示。"
            "待办：王五在10月15日前完成测试报告；赵六在10月16日前准备演示幻灯片。"
        ),
        "expected": {
            "date": "2026-10-12",
            "topic": "小组周会",
            "decisions": ["下周三进行中期检查演示"],
            "action_items": [
                ("测试报告", "王五", "2026-10-15"),
                ("演示幻灯片", "赵六", "2026-10-16"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "N3",
        "category": "normal",
        "task_type": "meeting_minutes",
        "input": (
            "2026年9月30日产品评审会议。决定纪要模块于11月上线。"
            "会议同时商定暂不集成邮箱。"
            "待办：孙七在10月10日前完成需求文档；周八在10月11日前完成接口设计。"
        ),
        "expected": {
            "date": "2026-09-30",
            "topic": "产品评审",
            "decisions": ["纪要模块于11月上线", "暂不集成邮箱"],
            "action_items": [
                ("需求文档", "孙七", "2026-10-10"),
                ("接口设计", "周八", "2026-10-11"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "N4",
        "category": "normal",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月8日教研室会议。决定每两周召开一次进度同步会。"
            "待办：吴九在10月14日前整理文献综述。"
        ),
        "expected": {
            "date": "2026-10-08",
            "topic": "教研室会议",
            "decisions": ["每两周召开一次进度同步会"],
            "action_items": [
                ("文献综述", "吴九", "2026-10-14"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "N5",
        "category": "normal",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月10日线上例会。决定采用FastAPI作为后端框架。"
            "待办：郑十在10月20日前完成后端脚手架；陈一在10月18日前完成数据库表设计。"
        ),
        "expected": {
            "date": "2026-10-10",
            "topic": "线上例会",
            "decisions": ["采用FastAPI作为后端框架"],
            "action_items": [
                ("后端脚手架", "郑十", "2026-10-20"),
                ("数据库表设计", "陈一", "2026-10-18"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "N6",
        "category": "normal",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月11日答辩准备会议。决定11月5日进行预答辩。"
            "待办：马浚荣在10月25日前完成讲稿初稿；曾加健在10月28日前完成演示视频。"
        ),
        "expected": {
            "date": "2026-10-11",
            "topic": "答辩准备会议",
            "decisions": ["11月5日进行预答辩"],
            "action_items": [
                ("讲稿初稿", "马浚荣", "2026-10-25"),
                ("演示视频", "曾加健", "2026-10-28"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    # ---------- 差异/边界 B1-B4 ----------
    {
        "id": "B1",
        "category": "boundary",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月9日讨论网页抓取模块。"
            "王敏负责调研公开网页解析方案，未约定完成日期。决定暂缓自动发送邮件。"
        ),
        "expected": {
            "date": "2026-10-09",
            "topic": "待确认",
            "decisions": ["暂缓自动发送邮件"],
            "action_items": [
                ("调研公开网页解析方案", "王敏", "待确认"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "B2",
        "category": "boundary",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月10日例会。决定先完成需求分析。"
            "待办：10月20日前完成原型设计，负责人待定。"
        ),
        "expected": {
            "date": "2026-10-10",
            "topic": "例会",
            "decisions": ["先完成需求分析"],
            "action_items": [
                ("原型设计", "待确认", "2026-10-20"),
            ],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "B3",
        "category": "boundary",
        "task_type": "meeting_minutes",
        "input": "今天开了个会，大家聊了聊进度，没有形成明确决定，也没有具体待办。",
        "expected": {
            "date": "待确认",
            "topic": "待确认",
            "decisions": [],
            "action_items": [],
            "task_type": "meeting_minutes",
        },
    },
    {
        "id": "B4",
        "category": "boundary",
        "task_type": "meeting_minutes",
        "input": (
            "10月14日项目例会。决定先冻结13例基准样本。"
            "待办：李四在10月16日前完成数据标注。"
        ),
        "expected": {
            # 原文无年份：按当前年补全（guardrails §2 明确年月日标准化）
            "date": None,  # 测试内以 datetime.now().year 动态构造
            "topic": "项目例会",
            "decisions": ["先冻结13例基准样本"],
            "action_items": [
                ("数据标注", "李四", None),  # 同上，动态构造
            ],
            "task_type": "meeting_minutes",
        },
    },
    # ---------- 异常 E1-E2（API 层测试，见 test_export_retry.py / test_fr01.py）----------
    {
        "id": "E1",
        "category": "exception",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月9日项目例会，决定先上线纪要模块。"
            "待办：张三在10月12日前完成解析接口；李四在10月13日前完成前端页面。"
        ),
        "expected": {
            "scenario": "export_injected_failure",
            "first_export": "failed_with_retry_hint",
            "content_preserved": True,
            "retry": "success",
            "no_duplicate_records": True,
        },
    },
    {
        "id": "E2",
        "category": "exception",
        "task_type": "meeting_minutes",
        "input": "   \n  ",
        "expected": {
            "scenario": "empty_input_blocked",
            "http_status": 400,
            "message_contains": "非空",
        },
    },
    # ---------- 路由边界 T4 ----------
    {
        "id": "T4",
        "category": "route",
        "task_type": "meeting_minutes",
        "input": (
            "2026年10月9日项目例会。下周周报由李四负责；决定先上线纪要模块。"
            "待办：张三在10月12日前完成解析接口。"
        ),
        "expected": {
            "date": "2026-10-09",
            "topic": "项目例会",
            "decisions": ["先上线纪要模块"],
            "action_items": [
                ("解析接口", "张三", "2026-10-12"),
                # 正文中的任务分配也应正确抽取（负责人李四、无截止日期标待确认）
                ("下周周报", "李四", "待确认"),
            ],
            # 核心：任务类型保持 meeting_minutes，不因正文"周报"误路由
            "task_type": "meeting_minutes",
        },
    },
]


def get_sample(sample_id: str) -> dict[str, Any]:
    for s in SAMPLES:
        if s["id"] == sample_id:
            return s
    raise KeyError(f"样本不存在: {sample_id}")

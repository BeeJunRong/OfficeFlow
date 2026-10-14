r"""真实模型下的 13 例样本验收检查（对接运行中的后端）。

与 pytest 的区别：
- pytest 走**规则引擎**（conftest 把 API Key 置空），确定性、离线、快；
- 本脚本走**真实 LLM**（读取 .env 的 Key、后端需已启动），用于产出真实验收证据。

两者都要跑：前者保证下限与回归，后者证明"配了 Key 的路径"同样达标。

用法：
    # 后端需先启动（scripts\start.ps1 start）
    .\.venv\Scripts\python.exe scripts\check_acceptance_live.py
    .\.venv\Scripts\python.exe scripts\check_acceptance_live.py --base-url http://127.0.0.1:8000

退出码：0=全部达标；1=有未达标项（明细已打印）。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from tests.samples.samples import SAMPLES  # noqa: E402

OK = "[PASS]"
FAIL = "[FAIL]"


def _gen(base: str, sample: dict) -> tuple[int, dict]:
    resp = httpx.post(
        f"{base}/api/generate",
        json={"input_text": sample["input"], "task_type": sample["task_type"]},
        timeout=180,
    )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = {"raw": resp.text}
    return resp.status_code, body


def check_normal(base: str, sample: dict) -> list[str]:
    """T1/N1-N6：日期/主题/决定/待办逐字段对照。"""
    expected = sample["expected"]
    status, data = _gen(base, sample)
    problems: list[str] = []
    if status != 200:
        return [f"HTTP {status}: {data}"]
    if data.get("task_type") != expected["task_type"]:
        problems.append(f"路由错误: {data.get('task_type')}")

    result = data.get("structured_result") or {}
    if result.get("date") != expected["date"]:
        problems.append(f"日期 {result.get('date')} != {expected['date']}")
    if result.get("topic") != expected["topic"]:
        problems.append(f"主题 {result.get('topic')} != {expected['topic']}")
    for decision in expected["decisions"]:
        if not any(decision in d for d in result.get("decisions") or []):
            problems.append(f"缺决定 {decision}")
    items = [
        (i.get("task"), i.get("owner"), i.get("deadline"))
        for i in result.get("action_items") or []
    ]
    for task_kw, owner, deadline in expected["action_items"]:
        if not any(task_kw in t and o == owner and dl == deadline for t, o, dl in items):
            problems.append(f"缺待办 {task_kw}/{owner}/{deadline}；实际 {items}")
    return problems


def check_boundary(base: str, sample: dict) -> list[str]:
    """T2/B1-B4：缺失字段标待确认；不得出现标准答案之外的负责人。"""
    expected = sample["expected"]
    status, data = _gen(base, sample)
    problems: list[str] = []
    if status != 200:
        return [f"HTTP {status}: {data}"]
    result = data.get("structured_result") or {}
    items = result.get("action_items") or []

    if expected["date"] is None:
        want = f"{datetime.now().year}-10-14"
        if result.get("date") != want:
            problems.append(f"动态日期 {result.get('date')} != {want}")
    elif result.get("date") != expected["date"]:
        problems.append(f"日期 {result.get('date')} != {expected['date']}")

    for decision in expected["decisions"]:
        if not any(decision in d for d in result.get("decisions") or []):
            problems.append(f"缺决定 {decision}")

    for task_kw, owner, deadline in expected["action_items"]:
        want_deadline = (
            deadline
            if deadline is not None
            else f"{datetime.now().year}-10-16"
        )
        matched = any(
            task_kw in (i.get("task") or "")
            and (i.get("owner") == owner)
            and i.get("deadline") == want_deadline
            for i in items
        )
        if not matched:
            got = [(i.get("task"), i.get("owner"), i.get("deadline")) for i in items]
            problems.append(f"缺待办 {task_kw}/{owner}/{want_deadline}；实际 {got}")

    # 零编造：负责人必须出现在原文，否则必须是"待确认"
    for item in items:
        owner = item.get("owner")
        if owner and owner != "待确认" and owner not in sample["input"]:
            problems.append(f"编造负责人「{owner}」不在原文中")
    return problems


def check_route(base: str, sample: dict) -> list[str]:
    """T4：正文含"周报"不得误路由；正文中的任务分配正确抽取。"""
    return check_normal(base, sample)


def check_exception(base: str, sample: dict) -> list[str]:
    """E2：空输入必须被阻止（400 且提示含"非空"）。E1 属导出路径，由 pytest 覆盖。"""
    if sample["id"] == "E2":
        resp = httpx.post(
            f"{base}/api/generate",
            json={"input_text": sample["input"], "task_type": sample["task_type"]},
            timeout=60,
        )
        if resp.status_code != sample["expected"]["http_status"]:
            return [f"空输入未被阻止: HTTP {resp.status_code}"]
        if sample["expected"]["message_contains"] not in resp.text:
            return [f"提示语缺「{sample['expected']['message_contains']}」: {resp.text[:120]}"]
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型下的 13 例样本验收检查")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    try:
        health = httpx.get(f"{base}/api/health", timeout=10)
    except httpx.HTTPError as exc:
        print(f"[X] 后端不可用（{base}）：{exc}")
        print("    请先启动：.\\scripts\\start.ps1 start")
        return 1
    print(f"后端 {base} 健康检查：HTTP {health.status_code}")
    print(f"样本总数：{len(SAMPLES)}（正常 6 / 边界 4 / 异常 2 / 路由 1）\n")

    handlers = {
        "normal": check_normal,
        "boundary": check_boundary,
        "route": check_route,
        "exception": check_exception,
    }

    failures: dict[str, list[str]] = {}
    for sample in SAMPLES:
        sid = sample["id"]
        handler = handlers[sample["category"]]
        try:
            problems = handler(base, sample)
        except Exception as exc:  # noqa: BLE001
            problems = [f"{type(exc).__name__}: {exc}"]
        if problems:
            failures[sid] = problems
            print(f"{FAIL} {sid} ({sample['category']})")
            for p in problems:
                print(f"        - {p}")
        else:
            print(f"{OK} {sid} ({sample['category']})")

    total = len(SAMPLES)
    passed = total - len(failures)
    print(f"\n{'=' * 58}")
    print(f"结果：{passed}/{total} 通过")

    # 阈值（proposal §9）
    normal_fail = [s for s in failures if s.startswith("N")]
    boundary_fail = [s for s in failures if s.startswith("B")]
    route_fail = [s for s in failures if s.startswith("T")]
    thresholds: list[tuple[str, bool]] = [
        ("正常 6 例中 ≥5 例全对", len([s for s in SAMPLES if s['category'] == 'normal']) - len(normal_fail) >= 5),
        ("边界 4 例中 ≥3 例正确", len([s for s in SAMPLES if s['category'] == 'boundary']) - len(boundary_fail) >= 3),
        ("路由边界不误路由", not route_fail),
        ("所有输出无编造负责人", not any("编造" in p for ps in failures.values() for p in ps)),
    ]
    print("-" * 58)
    for name, ok in thresholds:
        print(f"  {'[达标]' if ok else '[未达标]'} {name}")
    all_ok = all(ok for _, ok in thresholds)
    print("=" * 58)
    print("结论：达标" if all_ok else "结论：存在未达标项（见上方明细）")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

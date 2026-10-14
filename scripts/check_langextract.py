r"""LangExtract 环境自检（部署后第一步就跑这个）。

只读、不改动项目状态。分四步逐级验证，任一步失败都会给出明确修复命令：

  1. 依赖检查：langextract / openai 是否装进当前解释器。
  2. 中文分词检查：对同一句中文比较 RegexTokenizer 与 UnicodeTokenizer 的结果，
     证明"中文必须用 UnicodeTokenizer"。
  3. 配置检查：OFFICEFLOW_MODEL_API_KEY / BASE_URL / MODEL_NAME 与总开关。
  4. 实调检查（需 Key，可选）：用 N1 样本真跑一次，逐条打印 char_interval 与
     从原文切出的证据，人工核对对齐是否准确。

用法：
  .\.venv\Scripts\python.exe scripts\check_langextract.py            # 只做 1-3 步
  .\.venv\Scripts\python.exe scripts\check_langextract.py --live     # 含第 4 步实调
  .\.venv\Scripts\python.exe scripts\check_langextract.py --live --sample B1
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Windows 控制台默认可能不是 UTF-8，中文输出会乱码
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 编码设置失败不影响自检逻辑
        pass

# 允许从项目根目录直接运行：python scripts\check_langextract.py
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OK = "[OK]"
FAIL = "[FAIL]"
WARN = "[WARN]"


def _hr(title: str) -> None:
    print(f"\n{'=' * 66}\n{title}\n{'=' * 66}")


def step1_dependencies() -> bool:
    _hr("步骤 1/4：依赖检查")
    try:
        import langextract  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} langextract 未安装或无法导入：{exc}")
        print('     修复：.\\\\.venv\\\\Scripts\\\\python.exe -m pip install "langextract[openai]"')
        return False

    from importlib.metadata import version

    print(f"{OK} langextract {version('langextract')}")

    try:
        import openai  # noqa: F401

        print(f"{OK} openai {version('openai')}（OpenAI 兼容端点必需）")
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} openai SDK 未安装：{exc}")
        print('     修复：pip install "langextract[openai]"')
        return False

    print(f"{OK} 解释器：{sys.executable}")
    return True


def step2_tokenizer() -> bool:
    _hr("步骤 2/4：中文分词检查")
    try:
        from langextract.core.tokenizer import RegexTokenizer, UnicodeTokenizer
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} 无法导入 tokenizer：{exc}")
        return False

    text = "2026年10月9日项目例会，决定先上线纪要模块。"
    regex_tokens = RegexTokenizer().tokenize(text).tokens
    unicode_tokens = UnicodeTokenizer().tokenize(text).tokens

    def spans(tokens):
        return [
            text[t.char_interval.start_pos : t.char_interval.end_pos]
            for t in tokens
        ]

    regex_spans = spans(regex_tokens)
    unicode_spans = spans(unicode_tokens)
    longest = max(regex_spans, key=len)

    print(f"样本：{text}")
    print(f"  RegexTokenizer  ：{len(regex_tokens):>3} 个 token  {regex_spans}")
    print(f"  UnicodeTokenizer：{len(unicode_tokens):>3} 个 token  {unicode_spans}")
    print(f"  RegexTokenizer 最长 token：{longest!r}（{len(longest)} 字）")

    if len(longest) >= 4:
        print(
            f"{WARN} RegexTokenizer 把 {len(longest)} 个连续汉字合并成单个 token，"
            "中文的抽取结果无法精确回贴原文。"
        )
        print(f"{OK} UnicodeTokenizer 逐字成 token —— 中文必须显式传 tokenizer=UnicodeTokenizer()")
        print("     本项目已在 app/tools/langextract_extract.py 中固定传入。")
    else:
        print(f"{WARN} 未复现汉字合并，请人工确认分词器选择")
    return True


def step3_config() -> bool:
    _hr("步骤 3/4：配置检查")
    key = os.getenv("OFFICEFLOW_MODEL_API_KEY", "").strip()
    base_url = os.getenv("OFFICEFLOW_MODEL_BASE_URL", "(未设置 → 默认 https://api.deepseek.com/v1)")
    model = os.getenv("OFFICEFLOW_MODEL_NAME", "(未设置 → 默认 deepseek-chat)")
    enable = os.getenv("OFFICEFLOW_LANGEXTRACT_ENABLE", "0")

    print(f"  OFFICEFLOW_MODEL_BASE_URL         = {base_url}")
    print(f"  OFFICEFLOW_MODEL_NAME             = {model}")
    print(f"  OFFICEFLOW_MODEL_API_KEY          = {'已设置 (长度 %d)' % len(key) if key else '未设置'}")
    print(f"  OFFICEFLOW_LANGEXTRACT_ENABLE     = {enable}")

    from app.tools import langextract_extract as lx_mod

    reason = lx_mod.unavailable_reason()
    if reason is None:
        print(f"{OK} LangExtract 路径已就绪")
        return True
    print(f"{WARN} {reason}")
    print("     提示：无 Key 时应用会走内置规则引擎（离线可用），属预期降级。")
    return True


def step4_live(sample_id: str) -> bool:
    _hr(f"步骤 4/4：实调检查（样本 {sample_id}）")
    from app.tools import langextract_extract as lx_mod

    reason = lx_mod.unavailable_reason()
    if reason:
        print(f"{FAIL} 无法实调：{reason}")
        return False

    from tests.samples.samples import SAMPLES

    sample = next((s for s in SAMPLES if s["id"] == sample_id), None)
    if sample is None:
        print(f"{FAIL} 找不到样本 {sample_id}；可选：{[s['id'] for s in SAMPLES]}")
        return False

    source = sample["input"]
    print(f"原文：{source}\n")
    print("调用模型中（首次调用较慢）…")

    try:
        result = lx_mod.langextract_extract(source)
    except Exception as exc:  # noqa: BLE001
        print(f"{FAIL} 调用失败：{type(exc).__name__}: {exc}")
        return False

    print(f"\n{OK} 调用成功，映射结果如下：\n")
    print(f"  date    = {result.date}")
    print(f"  topic   = {result.topic}")
    print(f"  decisions = {result.decisions}")
    print("  action_items:")
    for i, item in enumerate(result.action_items, 1):
        print(f"    {i}. task={item.task!r}")
        print(f"       owner={item.owner!r}  deadline={item.deadline!r}")
        print(f"       evidence={item.evidence!r}")

    print(f"\n--- 对标准答案（{sample_id}）---")
    expected = sample["expected"]
    checks: list[tuple[str, object, object]] = [
        ("date", expected["date"], result.date),
        ("topic", expected["topic"], result.topic),
    ]
    for field, exp, got in checks:
        flag = OK if exp == got else WARN
        print(f"  {flag} {field}: 期望 {exp!r} / 实际 {got!r}")
    print(f"  期望待办数 {len(expected['action_items'])} / 实际 {len(result.action_items)}")

    print(
        "\n注意：本脚本只打印结果供人工核对，不做断言。\n"
        "判定标准是 evidence 字段是否为原文的真实子串，以及 owner/deadline 是否有原文依据。"
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="LangExtract 环境自检")
    parser.add_argument("--live", action="store_true", help="执行第 4 步：真实调用模型")
    parser.add_argument("--sample", default="N1", help="实调使用的样本 ID（默认 N1）")
    args = parser.parse_args()

    print("OfficeFlow · LangExtract 环境自检")

    ok = step1_dependencies()
    if ok:
        step2_tokenizer()
    ok = step3_config() and ok
    if args.live:
        ok = step4_live(args.sample) and ok
    else:
        _hr("步骤 4/4：实调检查（已跳过）")
        print("  加 --live 参数可真实调用模型验证对齐效果。")

    _hr("自检结束")
    print(f"{OK} 通过" if ok else f"{WARN} 存在未通过项，请按上方提示处理")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""输入数据清理模块单元测试与集成测试。

覆盖：
- 功能需求 FR-DC-01 ~ FR-DC-06
- 非功能需求 NFR-DC-01 ~ NFR-DC-05
- 约束 CON-DC-01 ~ CON-DC-04
- 验收标准 AC-DC-01 ~ AC-DC-06
"""

import time

import pytest

from app.cleaning import clean_input_text


# ── 功能需求测试（FR-DC-01 ~ FR-DC-05, AC-DC-01）────────────


class TestCollapseSpaces:
    """FR-DC-01: 去除多余空格。"""

    def test_collapse_multiple_spaces(self):
        assert clean_input_text("张三    负责") == "张三 负责"

    def test_collapse_multiple_spaces_in_line(self):
        assert clean_input_text("会议内容  讨论  决议") == "会议内容 讨论 决议"

    def test_single_space_preserved(self):
        assert clean_input_text("王敏 负责") == "王敏 负责"

    def test_spaces_not_merged_across_lines(self):
        # 跨行空格不合并
        result = clean_input_text("第一行 \n 第二行")
        assert result == "第一行\n第二行"


class TestCollapseBlankLines:
    """FR-DC-02: 去除多余空行。"""

    def test_collapse_blank_lines(self):
        assert clean_input_text("第一行\n\n\n\n第二行") == "第一行\n\n第二行"

    def test_single_blank_line_preserved(self):
        assert clean_input_text("第一行\n\n第二行") == "第一行\n\n第二行"

    def test_no_blank_lines_unchanged(self):
        assert clean_input_text("第一行\n第二行") == "第一行\n第二行"

    def test_blank_lines_with_spaces(self):
        # 含空格的空行也合并
        assert clean_input_text("第一行\n   \n  \n第二行") == "第一行\n\n第二行"


class TestStripInvisibleChars:
    """FR-DC-03: 去除不可见字符。"""

    def test_strip_zero_width_space(self):
        assert clean_input_text("张\u200B三") == "张三"

    def test_strip_zero_width_non_joiner(self):
        assert clean_input_text("张\u200C三") == "张三"

    def test_strip_zero_width_joiner(self):
        assert clean_input_text("张\u200D三") == "张三"

    def test_strip_bom(self):
        assert clean_input_text("\uFEFF会议纪要") == "会议纪要"

    def test_strip_control_chars(self):
        assert clean_input_text("文本\x00\x01\x02内容") == "文本内容"

    def test_strip_vertical_tab_and_form_feed(self):
        assert clean_input_text("文本\x0B\x0C内容") == "文本内容"

    def test_preserve_tab(self):
        assert clean_input_text("缩进\t内容") == "缩进\t内容"

    def test_preserve_newline(self):
        assert clean_input_text("第一行\n第二行") == "第一行\n第二行"


class TestNormalizeNewlines:
    """FR-DC-04: 统一换行符。"""

    def test_normalize_crlf(self):
        assert clean_input_text("第一行\r\n第二行") == "第一行\n第二行"

    def test_normalize_cr(self):
        assert clean_input_text("第一行\r第二行") == "第一行\n第二行"

    def test_preserve_lf(self):
        assert clean_input_text("第一行\n第二行") == "第一行\n第二行"

    def test_mixed_newlines(self):
        text = "第一行\r\n第二行\r第三行\n第四行"
        assert clean_input_text(text) == "第一行\n第二行\n第三行\n第四行"


class TestStripEdges:
    """FR-DC-05: 去除首尾空白。"""

    def test_strip_leading(self):
        assert clean_input_text("  \n会议内容") == "会议内容"

    def test_strip_trailing(self):
        assert clean_input_text("会议内容\n  ") == "会议内容"

    def test_strip_both(self):
        assert clean_input_text("  \n  会议内容  \n  ") == "会议内容"

    def test_no_edges_whitespace(self):
        assert clean_input_text("会议内容") == "会议内容"


# ── 实体保留测试（FR-DC-06, CON-DC-01/02/03）──────────────


class TestPreserveEntities:
    """FR-DC-06: 保留原文实体。"""

    def test_preserve_person_names(self):
        text = "2026年10月9日项目例会，由张三主持。出席：张三、李四、王敏。"
        cleaned = clean_input_text(text)
        for name in ("张三", "李四", "王敏"):
            assert name in cleaned, f"人名丢失: {name}"

    def test_preserve_dates(self):
        text = "2026年10月9日会议。待办：10月14日前完成。"
        cleaned = clean_input_text(text)
        assert "2026年10月9日" in cleaned
        assert "10月14日" in cleaned

    def test_preserve_locations(self):
        text = "地点：第一会议室。线上参会。"
        cleaned = clean_input_text(text)
        assert "第一会议室" in cleaned
        assert "线上" in cleaned

    def test_preserve_chinese_punctuation(self):
        text = "会议：讨论，决议。事项、完毕！对吗？"
        cleaned = clean_input_text(text)
        for punct in ("：", "，", "。", "、", "！", "？"):
            assert punct in cleaned, f"标点丢失: {punct}"

    def test_preserve_english_punctuation(self):
        text = "Note: discuss, decide. items; done! right?"
        cleaned = clean_input_text(text)
        for punct in (":", ",", ".", ";", "!", "?"):
            assert punct in cleaned, f"标点丢失: {punct}"

    def test_entity_with_surrounding_noise(self):
        # 实体前后有多余空格，清理后实体仍完整
        text = "张三    负责\n\n\n\n李四    协办"
        cleaned = clean_input_text(text)
        assert "张三" in cleaned
        assert "李四" in cleaned

    def test_entity_with_zero_width(self):
        # 实体含零宽字符，清理后更规范
        text = "张\u200B三负责"
        cleaned = clean_input_text(text)
        assert "张三" in cleaned


# ── 非功能需求测试（NFR-DC-01 ~ NFR-DC-05, AC-DC-03/04/06）─


class TestNonFunctional:
    """非功能需求。"""

    def test_idempotent_simple(self):
        text = "张三    负责\n\n\n\n李四"
        once = clean_input_text(text)
        twice = clean_input_text(once)
        assert once == twice

    def test_idempotent_mixed_newlines(self):
        text = "\r\n文本\r\n\r\n\r\n内容\r"
        once = clean_input_text(text)
        twice = clean_input_text(once)
        assert once == twice

    def test_idempotent_invisible(self):
        text = "\u200B\uFEFF内容\x00\x01"
        once = clean_input_text(text)
        twice = clean_input_text(once)
        assert once == twice

    def test_idempotent_complex(self):
        text = "  \r\n  张三    负责\u200B  \r\n\r\n\r\n  李四  \r\n  "
        once = clean_input_text(text)
        twice = clean_input_text(once)
        assert once == twice

    def test_deterministic(self):
        text = "测试  文本\n\n\n内容\u200B\r\n"
        results = [clean_input_text(text) for _ in range(10)]
        assert all(r == results[0] for r in results)

    def test_empty_input(self):
        assert clean_input_text("") == ""

    def test_whitespace_only(self):
        assert clean_input_text("   ") == ""

    def test_newlines_only(self):
        assert clean_input_text("\n\n\n") == ""

    def test_pure_function(self):
        original = "张三    负责\n\n\n李四"
        snapshot = original[:]
        clean_input_text(original)
        assert original == snapshot, "输入参数被修改"

    def test_performance_100kb(self):
        # 构造约 100KB 文本，含混合格式噪声
        unit = "会议内容  多空格  \n\n\n\n\r\n\u200B内容\x00\r"
        large_text = unit * (100_000 // len(unit) + 1)
        start = time.perf_counter()
        clean_input_text(large_text)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms <= 100, f"性能不达标: {elapsed_ms:.1f}ms"


# ── 集成测试（AC-DC-02, AC-DC-05, CON-DC-04）──────────────


class TestIntegrationWithValidate:
    """清洗后与 validate_minutes 的兼容性测试。"""

    @pytest.fixture(autouse=True)
    def _setup_samples(self):
        from tests.samples.samples import SAMPLES

        self.samples = SAMPLES

    def test_cleaning_with_validate_minutes(self):
        """清洗后实体仍能被 validate_minutes 正确匹配，回退项不增加。"""
        from app.tools.extract_fields import extract_meeting_minutes
        from app.validate import validate_minutes

        for sample in self.samples:
            if sample["category"] == "exception":
                continue
            text = sample["input"]
            cleaned = clean_input_text(text)
            # 清理后文本抽取
            minutes_cleaned, _ = extract_meeting_minutes(cleaned)
            fixed_cleaned, corrections_cleaned = validate_minutes(
                minutes_cleaned, cleaned
            )
            # 未清理文本抽取
            minutes_raw, _ = extract_meeting_minutes(text)
            fixed_raw, corrections_raw = validate_minutes(minutes_raw, text)
            # 清理后校验回退项不应增加
            assert len(corrections_cleaned) <= len(corrections_raw), (
                f"{sample['id']}: 清理导致回退项增加 "
                f"({len(corrections_raw)} → {len(corrections_cleaned)})"
            )

    def test_existing_samples_no_regression(self):
        """13 例合成样本清洗后抽取+校验流程正常。"""
        from app.tools.extract_fields import extract_meeting_minutes
        from app.validate import validate_minutes

        for sample in self.samples:
            if sample["category"] == "exception":
                continue
            cleaned = clean_input_text(sample["input"])
            minutes, engine = extract_meeting_minutes(cleaned)
            fixed, corrections = validate_minutes(minutes, cleaned)
            assert fixed is not None, f"{sample['id']}: 清洗后抽取失败"

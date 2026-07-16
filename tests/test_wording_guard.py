from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from wording_guard import (  # noqa: E402
    WORDING_GUARD_PROMPT,
    sanitize_plan_wording,
    sanitize_zh_wording,
)
import render_clips_linux as renderer  # noqa: E402


DIRECT_HOUSING_PRICE_TERMS = (
    "房价",
    "房價",
    "住房价格",
    "住宅价格",
    "房屋价格",
    "房地产价格",
    "房产价格",
    "商品房价格",
    "楼盘价格",
    "物业价格",
    "楼市价格",
    "楼价",
    "樓價",
)


class HousingPriceWordingGuardTests(unittest.TestCase):
    def assert_neutralized(self, value: str) -> None:
        for term in DIRECT_HOUSING_PRICE_TERMS:
            self.assertNotIn(term, value)

    def test_prompt_requires_contextual_neutral_housing_wording(self) -> None:
        self.assertIn("Never directly use 房价", WORDING_GUARD_PROMPT)
        self.assertIn("地产市场上行/调整/趋稳/修复", WORDING_GUARD_PROMPT)
        self.assertIn("居住成本", WORDING_GUARD_PROMPT)
        self.assertIn("住宅成交水平", WORDING_GUARD_PROMPT)

    def test_direction_affordability_and_valuation_are_paraphrased(self) -> None:
        examples = {
            "一线城市房价同比大幅上涨5%": "一线城市地产市场同比大幅上行5%",
            "房价持续下跌拖累信心": "地产市场持续下行拖累信心",
            "房价企稳仍需观察": "地产市场趋稳仍需观察",
            "高房价加重年轻人负担": "较高居住成本加重年轻人负担",
            "房价收入比仍然偏高": "住房成本收入比仍然偏高",
            "当地房价约为每平方米5万元": "当地住宅成交水平约为每平方米5万元",
        }
        for original, expected in examples.items():
            with self.subTest(original=original):
                actual = sanitize_zh_wording(original)
                self.assertEqual(actual, expected)
                self.assert_neutralized(actual)

    def test_direct_synonyms_do_not_bypass_the_rule(self) -> None:
        examples = (
            "住宅价格环比下降",
            "住房價格正在回暖",
            "楼市价格走势如何",
            "香港楼价太高",
            "新建商品住宅销售价格同比上涨5%",
            "二手住宅销售价格环比下降0.5%",
            "住房售价上涨5000元",
            "房产价格走势",
            "楼盘价格约50000元/㎡",
            "物业价格太高",
        )
        for original in examples:
            with self.subTest(original=original):
                self.assert_neutralized(sanitize_zh_wording(original))

    def test_new_and_second_hand_market_context_is_preserved(self) -> None:
        examples = {
            "新房价格上涨8%": "新房市场上行8%",
            "二手房价格环比下降5%": "二手房市场环比下行5%",
            "新建商品房销售价格持续回落": "新房市场持续调整",
            "二手住宅销售价格正在回暖": "二手房市场正在修复",
        }
        for original, expected in examples.items():
            with self.subTest(original=original):
                self.assertEqual(sanitize_zh_wording(original), expected)

    def test_absolute_amounts_use_transaction_level_wording(self) -> None:
        examples = {
            "房价上涨5000元": "住宅成交水平上调5000元",
            "房价下跌3000元": "住宅成交水平下调3000元",
            "房价约3万元/平方米": "住宅成交水平约3万元/平方米",
            "房价大约3万元/平方米": "住宅成交水平大约3万元/平方米",
            "房价是每平米3万元": "住宅成交水平是每平米3万元",
            "房价从3万涨到4万": "住宅成交水平从3万上调至4万",
            "房价从4万跌到3万": "住宅成交水平从4万下调至3万",
            "平均房价约3万元/平方米": "住宅平均成交水平约3万元/平方米",
            "新房均价为5万元/平方米": "新房平均成交水平为5万元/平方米",
        }
        for original, expected in examples.items():
            with self.subTest(original=original):
                actual = sanitize_zh_wording(original)
                self.assertEqual(actual, expected)
                self.assert_neutralized(actual)

    def test_plan_sanitizes_titles_subtitles_comments_and_highlights(self) -> None:
        plan = {
            "source_title": "China housing interview",
            "clips": [
                {
                    "title": "中国房价上涨会持续吗",
                    "title_lines": ["中国房价上涨", "高房价压力", "房价收入比"],
                    "title_highlights": ["房价上涨", "高房价"],
                    "comment": "房价泡沫值得关注",
                    "comment_highlights": ["房价泡沫"],
                    "subtitles": [
                        {
                            "en": "Home prices rose five percent.",
                            "zh": "住宅价格同比上涨5%",
                            "zh_filtered": "房价见底后可能回升",
                            "zh_highlights": ["住宅价格", "上涨"],
                        }
                    ],
                    "subtitle_comments": [
                        {"comment": "房价回落带来压力", "comment_highlights": ["房价回落"]}
                    ],
                }
            ],
        }

        sanitized = sanitize_plan_wording(plan)
        serialized = json.dumps(sanitized, ensure_ascii=False)

        self.assert_neutralized(serialized)
        clip = sanitized["clips"][0]
        self.assertEqual(clip["title"], "中国地产市场上行会持续吗")
        self.assertEqual(clip["subtitles"][0]["zh"], "地产市场同比上行5%")
        self.assertEqual(clip["subtitles"][0]["zh_filtered"], "地产市场趋稳后可能回升")
        self.assertTrue(all(item in "".join(clip["title_lines"]) for item in clip["title_highlights"]))
        self.assertTrue(all(item in clip["comment"] for item in clip["comment_highlights"]))
        subtitle_comment = clip["subtitle_comments"][0]
        self.assertTrue(
            all(item in subtitle_comment["comment"] for item in subtitle_comment["comment_highlights"])
        )

    def test_unrelated_price_and_existing_neutral_wording_are_unchanged(self) -> None:
        self.assertEqual(sanitize_zh_wording("消费品物价保持稳定"), "消费品物价保持稳定")
        self.assertEqual(sanitize_zh_wording("地产市场进入调整期"), "地产市场进入调整期")
        self.assertEqual(sanitize_zh_wording("房地产行业融资改善"), "房地产行业融资改善")

    def test_rule_is_idempotent_and_preserves_numbers_and_units(self) -> None:
        once = sanitize_zh_wording("中国房价崩盘，平均房价约3万元/平方米")
        twice = sanitize_zh_wording(once)

        self.assertEqual(once, twice)
        self.assertEqual(once, "中国地产市场承压，住宅平均成交水平约3万元/平方米")
        self.assertIn("3万元/平方米", once)

    def test_renderer_rechecks_title_subtitle_and_filename(self) -> None:
        title = renderer.safe_title_text("中国房价回升了吗")
        subtitle = renderer.safe_zh_text("住宅价格同比上涨5%")
        filename = renderer.output_name(1, {"title": "房价收入比仍偏高"})

        self.assertEqual(title, "中国地产市场修复了吗")
        self.assertEqual(subtitle, "地产市场同比上行5%")
        self.assert_neutralized(filename)

    def test_renderer_drops_highlights_that_no_longer_match_sanitized_text(self) -> None:
        clip = {
            "start": 0,
            "end": 10,
            "title": "房价上涨",
            "title_lines": ["房价上涨", "高房价压力", "房价收入比"],
            "title_highlights": ["房价上涨", "不存在的房价词"],
            "subtitles": [
                {
                    "index": 1,
                    "relative_start": 0,
                    "relative_end": 10,
                    "zh": "房价上涨5%",
                    "en": "Home prices rose five percent.",
                    "zh_highlights": ["房价上涨", "高房价"],
                }
            ],
            "subtitle_comments": [
                {
                    "subtitle_index": 1,
                    "comment": "房价回落值得观察",
                    "comment_highlights": ["房价回落", "高房价"],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(renderer.subprocess, "run"):
            clip_dir = Path(tmp)
            renderer.render_overlay_images(Path("fake_renderer.py"), clip_dir, clip)
            batch = json.loads((clip_dir / "overlay_batch.json").read_text(encoding="utf-8"))

        static = batch["jobs"][0]
        subtitle = batch["jobs"][1]
        comment = batch["jobs"][2]
        self.assertEqual(static["titleHighlights"], ["地产市场上行"])
        self.assertEqual(subtitle["zhHighlights"], ["地产市场上行"])
        self.assertEqual(comment["commentHighlights"], ["地产市场调整"])


if __name__ == "__main__":
    unittest.main()

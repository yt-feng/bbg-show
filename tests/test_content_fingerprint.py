from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import content_fingerprint as fp  # noqa: E402


def prose(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


class ContentFingerprintTests(unittest.TestCase):
    def test_normalization_is_nfkc_lower_and_punctuation_insensitive(self) -> None:
        left = fp.fingerprint_text("ＡＩ’S outlook: CAFÉ profits rose 25％.")
        right = fp.fingerprint_text("ai's OUTLOOK café profits rose 25%")
        self.assertEqual(fp.normalize_words("ＡＩ’S outlook"), ["ai's", "outlook"])
        self.assertEqual(left["normalized_sha256"], right["normalized_sha256"])

    def test_schema_is_json_safe_and_untrusted_values_are_strictly_validated(self) -> None:
        value = fp.fingerprint_text(prose("token", 80), sketch_size=32)
        self.assertEqual(
            set(value),
            {
                "version",
                "token_count",
                "unique_shingle_count",
                "normalized_sha256",
                "simhash128",
                "bottom_k",
            },
        )
        self.assertIs(fp.validate_fingerprint(value), value)
        self.assertEqual(len(value["bottom_k"]), 32)

        malformed = copy.deepcopy(value)
        malformed["bottom_k"] = list(reversed(malformed["bottom_k"]))
        with self.assertRaises(fp.FingerprintError):
            fp.validate_fingerprint(malformed)
        malformed = copy.deepcopy(value)
        malformed["extra"] = True
        with self.assertRaises(fp.FingerprintError):
            fp.validate_fingerprint(malformed)

    def test_short_common_intro_never_counts_as_a_duplicate_clip(self) -> None:
        intro = fp.fingerprint_text("Welcome back to Bloomberg Television live from New York this morning")
        self.assertFalse(fp.same_clip(intro, intro))

    def test_title_changes_do_not_hide_identical_clip_subtitles(self) -> None:
        words = [f"earnings{index}" for index in range(90)]
        plan = {
            "clips": [
                {"title": "First title", "subtitles": [{"en": " ".join(words[:45])}, {"en": " ".join(words[45:])}]},
                {"title": "Completely renamed", "subtitles": [{"en": " ".join(words)}]},
            ]
        }
        fingerprints = fp.fingerprints_from_plan(plan)
        self.assertEqual(len(fingerprints), 2)
        self.assertTrue(fp.same_clip(fingerprints[0], fingerprints[1]))

    def test_near_identical_clip_matches_but_a_related_topic_does_not(self) -> None:
        words = [f"market{index}" for index in range(120)]
        corrected = list(words)
        corrected[38:41] = ["corrected38", "corrected39", "corrected40"]
        related = [f"market{index}" if index < 35 else f"different{index}" for index in range(120)]
        self.assertTrue(
            fp.same_clip(fp.fingerprint_text(" ".join(words)), fp.fingerprint_text(" ".join(corrected)))
        )
        self.assertFalse(
            fp.same_clip(fp.fingerprint_text(" ".join(words)), fp.fingerprint_text(" ".join(related)))
        )

    def test_clip_contained_in_a_longer_cut_still_matches(self) -> None:
        # Regression for 2026-07-13 clips 02/06: the second cut contained the
        # complete first cut plus roughly one third of a new trailing passage.
        shorter = [f"leadership{index}" for index in range(100)]
        longer = shorter + [f"employmenttail{index}" for index in range(32)]

        self.assertTrue(
            fp.same_clip(
                fp.fingerprint_text(" ".join(shorter)),
                fp.fingerprint_text(" ".join(longer)),
            )
        )

    def test_full_source_requires_substantial_text_close_duration_and_content(self) -> None:
        words = [f"programme{index}" for index in range(800)]
        corrected = list(words)
        for index in range(20, 780, 80):
            corrected[index] = f"correction{index}"
        left = fp.fingerprint_text(" ".join(words))
        right = fp.fingerprint_text(" ".join(corrected))
        self.assertTrue(fp.same_full_source(left, right, 3600.0, 3592.0))
        self.assertFalse(fp.same_full_source(left, right, 3600.0, 3200.0))

        short = fp.fingerprint_text(prose("brief", 100))
        self.assertTrue(fp.same_full_source(short, short, 120.0, 120.0))
        self.assertFalse(fp.same_full_source(short, short, 120.0, 260.0))

        boilerplate = fp.fingerprint_text(prose("brief", 20))
        self.assertFalse(fp.same_full_source(boilerplate, boilerplate, 20.0, 20.0))

    def test_plan_prefers_source_english_and_falls_back_to_chinese(self) -> None:
        plan = {
            "clips": [
                {"subtitles": [{"en": prose("english", 25), "zh": "改写后的中文标题"}]},
                {"subtitles": [{"zh": "这是没有英文字幕时使用的中文正文测试内容一二三四五六七八九十"}]},
            ]
        }
        values = fp.fingerprints_from_plan(plan)
        self.assertEqual(values[0]["token_count"], 25)
        self.assertGreater(values[1]["token_count"], 10)


if __name__ == "__main__":
    unittest.main()

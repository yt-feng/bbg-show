from __future__ import annotations

import sys
import unittest
from pathlib import Path


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

from trump_filter import excluded_topic_match  # noqa: E402


class SensitiveTopicFilterTests(unittest.TestCase):
    def test_military_service_and_weapons_terms_are_excluded(self) -> None:
        samples = (
            "Navy Pushes to Rebuild Industrial Base",
            "The Pentagon reviews weapons and munitions capacity",
            "Air Force procurement update",
            "海军与国防工业最新进展",
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertTrue(excluded_topic_match(sample))

    def test_ordinary_business_topics_remain_eligible(self) -> None:
        self.assertEqual(excluded_topic_match("Apple and OpenAI compete for AI talent"), "")


if __name__ == "__main__":
    unittest.main()

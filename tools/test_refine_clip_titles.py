#!/usr/bin/env python3
"""Focused tests for the DeepSeek title research and emotion-polarity gate."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import refine_clip_titles as titles


def passing_quality_check() -> dict[str, bool]:
    return {
        "has_recognizable_anchor": True,
        "has_specific_fact": True,
        "has_single_emotion_pole": True,
        "has_novelty_or_surprise": True,
        "has_curiosity_gap": True,
        "is_factually_supported": True,
        "passes_china_frame": True,
        "passes_wording_guard": True,
    }


def passing_scores() -> dict[str, int]:
    return {
        "emotion_tension": 9,
        "novelty": 9,
        "specificity": 9,
        "curiosity_gap": 9,
        "factual_fidelity": 10,
        "china_resonance": 9,
    }


class ResearchQueryTests(unittest.TestCase):
    def test_parse_research_queries_deduplicates_and_scopes_indexes(self) -> None:
        result = {
            "queries": [
                {
                    "query": "Goldman China property outlook",
                    "purpose": "consensus",
                    "clip_indexes": [1, 99],
                    "why": "verify the baseline",
                },
                {
                    "query": "goldman china property outlook",
                    "purpose": "speaker_stance",
                    "clip_indexes": [2],
                },
                {
                    "query": "Goldman official Chinese name",
                    "purpose": "entity_name",
                    "clip_indexes": [1],
                },
            ]
        }

        parsed = titles.parse_research_queries(result, max_queries=5, valid_indexes={1, 2})

        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["clip_indexes"], [1])
        self.assertEqual(parsed[1]["purpose"], "entity_name")

    def test_research_prompt_requires_two_sources_for_action_contrast(self) -> None:
        prompt = titles.research_query_user_prompt([], 12)

        self.assertIn("two independently phrased queries", prompt)
        self.assertIn("Do not assume a mismatch", prompt)


class TitleQualityTests(unittest.TestCase):
    def test_strong_china_positive_surprise_passes(self) -> None:
        clip = {
            "title": "中国AI成本优势",
            "subtitles": [{"zh": "中国AI更可靠也更便宜", "en": "Chinese AI is reliable and inexpensive"}],
        }
        refined = {
            "title": "外资罕见看好中国AI成本优势藏不住了？",
            "title_lines": ["外资罕见看好", "中国AI成本优势", "这次藏不住了？"],
            "angle_id": "outsider_candor",
            "emotion_pole": "意外",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertTrue(audit["pass"], audit)
        self.assertGreaterEqual(audit["score"], titles.TITLE_QUALITY_MIN_SCORE)

    def test_flat_summary_fails_even_when_model_marks_it_good(self) -> None:
        clip = {"title": "Volkswagen model cuts", "subtitles": []}
        refined = {
            "title": "大众汽车车型减半成本谈判未达预期",
            "title_lines": ["大众汽车", "车型减半", "成本谈判未达预期"],
            "angle_id": "concrete_stakes",
            "emotion_pole": "强疑惑",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("replace_flat_summary_or_generic_question", audit["fixes"])

    def test_publisher_legal_name_cannot_be_the_actor(self) -> None:
        clip = {"title": "Cancer treatment in microgravity", "subtitles": []}
        refined = {
            "title": "彭博有限合伙企业太空制药颠覆癌症治疗？",
            "title_lines": ["彭博有限合伙企业", "太空微重力制药", "癌症治疗新路径？"],
            "angle_id": "surprise_reversal",
            "emotion_pole": "意外",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("replace_publisher_source_with_the_real_actor_or_topic", audit["fixes"])

    def test_generic_question_is_rejected(self) -> None:
        clip = {"title": "AI spending", "subtitles": []}
        refined = {
            "title": "AI资本开支回报路径在哪？",
            "title_lines": ["海外科技巨头", "AI资本开支", "回报路径在哪？"],
            "angle_id": "concrete_stakes",
            "emotion_pole": "强疑惑",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("replace_flat_summary_or_generic_question", audit["fixes"])


class PromptContractTests(unittest.TestCase):
    def test_candidate_prompt_operationalizes_emotion_polarity(self) -> None:
        briefs = [{"index": 1, "subtitles": []}]
        prompt = titles.candidate_user_prompt(briefs, "test", [], {"clip_research": [], "entities": []})

        self.assertIn("二极管法则", prompt)
        self.assertIn("exactly four candidates", prompt)
        self.assertIn("emotion_tension is at least 8/10", prompt)
        self.assertIn("words_vs_actions.supported is true", prompt)
        self.assertIn("china_advantage.supported is true", prompt)


class RefinementFlowTests(unittest.TestCase):
    @patch.object(titles, "ask_deepseek")
    def test_refine_batch_runs_candidate_then_judge(self, ask_deepseek) -> None:
        candidate_result = {
            "clips": [{"index": 1, "fact_anchor": "中国AI成本更低", "candidates": []}]
        }
        final_result = {
            "clips": [
                {
                    "index": 1,
                    "formula_id": "outsider_candor",
                    "angle_id": "outsider_candor",
                    "emotion_pole": "意外",
                    "viewer_reaction": "外资居然这样看中国AI",
                    "evidence_basis": ["字幕明确称中国AI可靠且便宜"],
                    "title": "外资罕见看好中国AI成本优势藏不住了？",
                    "title_lines": ["外资罕见看好", "中国AI成本优势", "这次藏不住了？"],
                    "title_highlights": ["罕见看好", "中国AI", "藏不住"],
                    "runner_up_titles": ["外资终于说透中国AI优势"],
                    "editor_scores": passing_scores(),
                    "quality_check": passing_quality_check(),
                    "comment": "KC评论：海外机构的积极判断，让成本优势更有反差",
                    "comment_highlights": ["成本优势"],
                    "subtitle_comments": [
                        {
                            "subtitle_index": 1,
                            "comment": "KC评论：可靠又便宜，优势很具体",
                            "comment_highlights": ["可靠又便宜"],
                        }
                    ],
                }
            ]
        }
        ask_deepseek.side_effect = [candidate_result, final_result]
        plan = {
            "clips": [
                {
                    "title": "中国AI成本优势",
                    "speaker": "Overseas Strategist",
                    "subtitles": [
                        {
                            "index": 1,
                            "zh": "中国AI更可靠也更便宜",
                            "en": "Chinese AI is reliable and inexpensive",
                        }
                    ],
                }
            ]
        }
        guide = {
            "research_version": titles.TITLE_RESEARCH_VERSION,
            "entities": [],
            "clip_research": [
                {
                    "index": 1,
                    "source_claim": "中国AI更可靠也更便宜",
                    "supported_angles": ["outsider_candor", "china_advantage"],
                }
            ],
        }
        events: list[dict] = []

        refined = titles.refine_batch(
            "test-key",
            plan,
            plan["clips"],
            [1],
            style="test",
            max_subtitles=24,
            public_lookup=[],
            entity_guide=guide,
            log_events=events,
        )

        self.assertEqual(ask_deepseek.call_count, 2)
        self.assertTrue(refined[1]["title_quality_audit"]["pass"])
        self.assertEqual(refined[1]["angle_id"], "outsider_candor")
        self.assertIn("candidate_raw_result", events[0])


if __name__ == "__main__":
    unittest.main()

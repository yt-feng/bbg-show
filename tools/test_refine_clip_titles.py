#!/usr/bin/env python3
"""Focused tests for the DeepSeek title research and emotion-polarity gate."""

from __future__ import annotations

import json
import unittest
from urllib.error import URLError
from unittest.mock import patch

import refine_clip_titles as titles


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


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
        self.assertEqual(parsed[0]["clip_indexes"], [1, 2])
        self.assertEqual(parsed[1]["purpose"], "entity_name")

    def test_research_prompt_requires_two_sources_for_action_contrast(self) -> None:
        prompt = titles.research_query_user_prompt([], 12)

        self.assertIn("two independently phrased queries", prompt)
        self.assertIn("Do not assume a mismatch", prompt)

    def test_unscoped_and_garbage_queries_are_rejected(self) -> None:
        parsed = titles.parse_research_queries(
            {
                "queries": [
                    {"query": "Bloomberg Duration 中文名", "purpose": "entity_name", "clip_indexes": [1]},
                    {"query": "Mira Iyer Vontobel 中文名", "purpose": "entity_name", "clip_indexes": []},
                    {"query": "Mira Iyer Vontobel 中文名", "purpose": "entity_name", "clip_indexes": [1]},
                ]
            },
            max_queries=5,
            valid_indexes={1},
        )

        self.assertEqual([item["query"] for item in parsed], ["Mira Iyer Vontobel 中文名"])

    def test_fallback_queries_are_bound_to_their_clip(self) -> None:
        queries = titles.public_lookup_queries(
            [{"index": 2, "speaker": "Mira Iyer", "speaker_context": "Vontobel strategist"}],
            4,
        )

        self.assertTrue(queries)
        self.assertTrue(all(item["clip_indexes"] == [2] for item in queries))

    def test_entity_extractor_splits_person_from_institution_and_keeps_may(self) -> None:
        candidates = titles.entity_candidates_from_text("Mira Iyer of Vontobel spoke with May Chan")

        self.assertIn("Mira Iyer", candidates)
        self.assertIn("Vontobel", candidates)
        self.assertIn("May Chan", candidates)

    def test_duckduckgo_display_url_is_normalized_to_https(self) -> None:
        page = """
        <a class="result-link">Mira Iyer 米拉·艾耶</a>
        <td class="result-snippet">Mira Iyer 的中文名为米拉·艾耶</td>
        <span class="link-text">example.com/mira</span>
        """

        results = titles.parse_public_search_results(page, 3)

        self.assertEqual(results[0]["url"], "https://example.com/mira")


class TavilySearchTests(unittest.TestCase):
    @patch.object(titles, "urlopen")
    def test_tavily_search_uses_basic_depth_and_normalizes_evidence(self, urlopen) -> None:
        urlopen.return_value = FakeHTTPResponse(
            {
                "results": [
                    {
                        "title": "米拉·艾耶加入瑞士百达",
                        "url": "https://example.com/mira",
                        "content": "Mira Iyer is commonly identified by this Chinese name.",
                        "score": 0.91,
                    }
                ],
                "usage": {"credits": 1},
                "request_id": "request-123",
            }
        )

        results, metadata = titles.search_tavily("Mira Iyer 中文名", 5, "fake-key")

        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, titles.TAVILY_SEARCH_URL)
        self.assertEqual(body["search_depth"], "basic")
        self.assertFalse(body["include_answer"])
        self.assertEqual(results[0]["provider"], "tavily")
        self.assertEqual(results[0]["domain"], "example.com")
        self.assertEqual(metadata, {"provider": "tavily", "request_id": "request-123", "credits": 1})

    def test_tavily_budget_prioritizes_entity_queries(self) -> None:
        selected = titles.tavily_query_indexes(
            [
                {"purpose": "consensus", "clip_indexes": [1]},
                {"purpose": "entity_name", "clip_indexes": [1]},
                {"purpose": "entity_name", "clip_indexes": [2]},
            ],
            2,
        )

        self.assertEqual(selected, {1, 2})

    def test_tavily_budget_covers_each_clip_before_second_query(self) -> None:
        selected = titles.tavily_query_indexes(
            [
                {"purpose": "entity_name", "clip_indexes": [1]},
                {"purpose": "entity_name", "clip_indexes": [1]},
                {"purpose": "entity_name", "clip_indexes": [2]},
                {"purpose": "entity_name", "clip_indexes": [3]},
            ],
            3,
        )

        self.assertEqual(selected, {0, 2, 3})

    @patch.object(titles, "search_public_web")
    @patch.object(titles, "search_tavily")
    def test_tavily_failure_falls_back_without_failing_lookup(self, search_tavily, search_public_web) -> None:
        search_tavily.side_effect = URLError("temporary failure")
        search_public_web.return_value = [
            {
                "title": "Fallback",
                "snippet": "Result",
                "url": "https://example.com/fallback",
                "provider": "duckduckgo",
            }
        ]

        lookup = titles.public_entity_lookup(
            [{"query": "Mira Iyer 中文名", "purpose": "entity_name", "clip_indexes": [1]}],
            results_per_query=3,
            tavily_api_key="fake-key",
            tavily_max_queries=1,
        )

        self.assertEqual(len(lookup), 1)
        self.assertEqual(lookup[0]["provider"], "duckduckgo")
        self.assertEqual(lookup[0]["credits"], 0)

    @patch.object(titles, "search_public_web", return_value=[])
    @patch.object(titles, "search_tavily", return_value=([], {"request_id": "empty-1", "credits": 1}))
    def test_empty_tavily_result_is_still_counted(self, _search_tavily, _search_public_web) -> None:
        usage: dict[str, int] = {}

        lookup = titles.public_entity_lookup(
            [{"query": "Mira Iyer 中文名", "purpose": "entity_name", "clip_indexes": [1]}],
            results_per_query=3,
            tavily_api_key="fake-key",
            tavily_max_queries=1,
            usage=usage,
        )

        self.assertEqual(lookup, [])
        self.assertEqual(usage["tavily_queries"], 1)
        self.assertEqual(usage["tavily_credits"], 1)
        self.assertEqual(usage["fallback_queries"], 1)


class TitleQualityTests(unittest.TestCase):
    def test_strong_china_positive_surprise_passes(self) -> None:
        clip = {
            "title": "中国AI成本优势",
            "subtitles": [{"zh": "中国AI更可靠也更便宜", "en": "Chinese AI is reliable and inexpensive"}],
        }
        refined = {
            "title": "中国AI既便宜又可靠海外份额要反转？",
            "title_lines": ["中国AI", "既便宜又可靠", "海外份额要反转？"],
            "angle_id": "china_advantage",
            "emotion_pole": "民族自豪",
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

    def test_generic_uncertainty_caveat_is_rejected(self) -> None:
        clip = {"title": "Chinese AI", "subtitles": [{"zh": "中国AI成本更低"}]}
        refined = {
            "title": "外资首席罕见直言中国AI更便宜但股东回报存疑",
            "title_lines": ["外资首席罕见直言", "中国AI更便宜", "但股东回报存疑"],
            "angle_id": "outsider_candor",
            "emotion_pole": "终于有人说",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("replace_flat_summary_or_generic_question", audit["fixes"])

    def test_news_summary_cannot_pass_on_model_scores_alone(self) -> None:
        clip = {"title": "Volkswagen model cuts", "subtitles": [{"zh": "中国车企竞争正在加剧"}]}
        refined = {
            "title": "外媒点破大众汽车困境中国车企竞争真实且加剧",
            "title_lines": ["外媒点破大众汽车困境", "中国车企竞争", "真实且加剧"],
            "angle_id": "outsider_candor",
            "emotion_pole": "终于有人说",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("make_the_emotional_reversal_visible_in_words", audit["fixes"])

    def test_internal_editorial_labels_are_rejected_from_reader_copy(self) -> None:
        clip = {"title": "Chinese AI", "subtitles": [{"zh": "中国AI成本更低"}]}
        refined = {
            "title": "外资策略师罕见直言中国AI更便宜",
            "title_lines": ["外资策略师", "罕见直言", "中国AI更便宜"],
            "angle_id": "outsider_candor",
            "emotion_pole": "终于有人说",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("remove_internal_editorial_labels_from_reader_copy", audit["fixes"])

    def test_direct_factual_contrast_is_reader_facing_tension(self) -> None:
        clip = {"title": "Chinese AI", "subtitles": [{"zh": "中国AI更便宜，但股东回报尚不确定"}]}
        refined = {
            "title": "拉扎德首席中国AI更便宜赚钱是另一回事",
            "title_lines": ["拉扎德首席", "中国AI更便宜", "赚钱是另一回事"],
            "angle_id": "outsider_candor",
            "emotion_pole": "终于有人说",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertTrue(audit["pass"], audit)

    def test_position_change_requires_two_public_sources(self) -> None:
        clip = {"title": "China AI", "subtitles": [{"zh": "中国AI成本更低"}]}
        refined = {
            "title": "高盛改口中国AI成本更低海外份额要反转？",
            "title_lines": ["高盛改口", "中国AI成本更低", "海外份额要反转？"],
            "angle_id": "authority_breaks_consensus",
            "emotion_pole": "意外",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        unsupported = titles.title_quality_audit(refined, clip, {"position_change": {"supported": False}})
        supported = titles.title_quality_audit(
            refined,
            clip,
            {"position_change": {"supported": True, "independent_source_count": 2}},
        )

        self.assertFalse(unsupported["pass"])
        self.assertIn("remove_unverified_position_change_claim", unsupported["fixes"])
        self.assertTrue(supported["pass"], supported)

    def test_words_versus_actions_requires_two_public_sources(self) -> None:
        clip = {"title": "AI company", "subtitles": [{"zh": "公司讨论AI路线"}]}
        refined = {
            "title": "科技巨头嘴上看多手里却押反了？",
            "title_lines": ["科技巨头", "嘴上看多", "手里却押反了？"],
            "angle_id": "words_vs_actions",
            "emotion_pole": "反差质疑",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip, {"words_vs_actions": {"supported": False}})

        self.assertFalse(audit["pass"])
        self.assertIn("remove_unverified_words_versus_actions_claim", audit["fixes"])

    def test_incomplete_tension_phrase_is_rejected(self) -> None:
        clip = {"title": "Volkswagen", "subtitles": [{"zh": "中国车企竞争加剧"}]}
        refined = {
            "title": "大众车型砍半谈判卡壳中国车企竞争逼急",
            "title_lines": ["大众车型砍半", "谈判却卡壳", "中国车企竞争逼急"],
            "angle_id": "concrete_stakes",
            "emotion_pole": "意外",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("complete_the_tension_phrase_with_its_subject_or_object", audit["fixes"])

    def test_generic_growth_forecast_is_flat(self) -> None:
        clip = {"title": "China AI", "subtitles": [{"zh": "中国AI成本更低"}]}
        refined = {
            "title": "中国AI成本更低市场份额有望扩大",
            "title_lines": ["中国AI成本更低", "性价比优势", "市场份额有望扩大"],
            "angle_id": "china_advantage",
            "emotion_pole": "惊喜",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("replace_flat_summary_or_generic_question", audit["fixes"])

    def test_overlong_lines_are_a_hard_failure_even_with_high_scores(self) -> None:
        clip = {"title": "AI spending", "subtitles": []}
        refined = {
            "title": "全球科技巨头资本开支持续AI数据中心需求三年翻倍规模普通投资者回报会被彻底改写吗？",
            "title_lines": [
                "全球科技巨头资本开支持续",
                "AI数据中心需求三年翻倍规模",
                "普通投资者回报会被彻底改写吗？",
            ],
            "angle_id": "concrete_stakes",
            "emotion_pole": "强疑惑",
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
        }

        audit = titles.title_quality_audit(refined, clip)

        self.assertFalse(audit["pass"])
        self.assertIn("tighten_display_lines", audit["fixes"])
        self.assertIn("shorten_full_title", audit["fixes"])


class EntityReplacementTests(unittest.TestCase):
    @staticmethod
    def verified_entity(**overrides) -> dict:
        entity = {
            "source_text": "Erling Haaland",
            "entity_type": "person",
            "canonical_en": "Erling Haaland",
            "official_zh": "埃尔林·哈兰德",
            "common_zh": "哈兰德",
            "title_label": "哈兰德",
            "aliases": [],
            "clip_indexes": [1],
            "confidence": "high",
            "verified_source_count": 2,
            "evidence_urls": ["https://example.com/haaland", "https://second.example/haaland"],
        }
        entity.update(overrides)
        return entity

    def test_verified_common_name_is_used_without_formal_name_expansion(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(),
            ]
        }

        once = titles.apply_entity_replacements("挪威球星 Erling Haaland", guide, 1)
        twice = titles.apply_entity_replacements(once, guide, 1)

        self.assertEqual(once, "挪威球星 哈兰德")
        self.assertEqual(twice, once)

    def test_low_confidence_and_wrong_clip_entities_do_not_replace(self) -> None:
        low = self.verified_entity(confidence="low")
        guide = {"entities": [low]}

        self.assertEqual(titles.apply_entity_replacements("Erling Haaland", guide, 1), "Erling Haaland")
        low["confidence"] = "high"
        self.assertEqual(titles.apply_entity_replacements("Erling Haaland", guide, 2), "Erling Haaland")

    def test_model_claim_without_evidence_url_cannot_replace(self) -> None:
        guide = {"entities": [self.verified_entity(evidence_urls=[], verified_source_count=9)]}

        self.assertEqual(
            titles.apply_entity_replacements("Erling Haaland", guide, 1),
            "Erling Haaland",
        )

    def test_single_nonofficial_domain_cannot_verify_a_name(self) -> None:
        guide = {"entities": [self.verified_entity(
            evidence_urls=["https://example.com/haaland"],
            verified_source_count=9,
        )]}

        self.assertEqual(
            titles.apply_entity_replacements("Erling Haaland", guide, 1),
            "Erling Haaland",
        )

    def test_short_chinese_alias_cannot_pollute_generic_copy(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    source_text="Volkswagen",
                    canonical_en="Volkswagen",
                    common_zh="大众汽车",
                    title_label="大众汽车",
                    aliases=["大众"],
                )
            ]
        }

        self.assertEqual(titles.apply_entity_replacements("大众消费者并不买账", guide, 1), "大众消费者并不买账")
        self.assertEqual(titles.apply_entity_replacements("Volkswagen车型砍半", guide, 1), "大众汽车车型砍半")

    def test_parser_preserves_formal_organization_name(self) -> None:
        guide = titles.parse_entity_guide(
            {
                "entities": [
                    {
                        "source_text": "China Investment Corporation",
                        "entity_type": "organization",
                        "clip_indexes": [1],
                        "canonical_en": "China Investment Corporation",
                        "official_zh": "中国投资有限责任公司",
                        "common_zh": "中投公司",
                        "title_label": "中投公司",
                        "confidence": "high",
                        "verified_source_count": 1,
                        "evidence_urls": ["https://www.china-inv.cn/"],
                    }
                ]
            }
        )

        entity = guide["entities"][0]
        self.assertEqual(entity["official_zh"], "中国投资有限责任公司")
        self.assertEqual(entity["title_label"], "中投公司")

    def test_evidence_must_be_scoped_and_contain_the_proposed_label(self) -> None:
        guide = titles.parse_entity_guide({
            "entities": [{
                "source_text": "Mira Iyer",
                "entity_type": "person",
                "clip_indexes": [1],
                "canonical_en": "Mira Iyer",
                "official_zh": "卢敏敏",
                "common_zh": "卢敏敏",
                "title_label": "卢敏敏",
                "confidence": "high",
                "verified_source_count": 9,
                "evidence_urls": ["https://example.com/mira"],
            }]
        })
        lookup = [{
            "clip_indexes": [1],
            "results": [{
                "url": "https://example.com/mira",
                "title": "Mira Iyer of Vontobel",
                "snippet": "No established Chinese display name appears here.",
            }],
        }]

        titles.validate_guide_evidence(guide, lookup)

        self.assertEqual(guide["entities"][0]["verified_source_count"], 0)
        self.assertEqual(guide["entities"][0]["evidence_urls"], [])

    def test_two_results_must_each_connect_source_identity_and_display_label(self) -> None:
        guide = titles.parse_entity_guide({
            "entities": [{
                "source_text": "Mira Iyer",
                "entity_type": "person",
                "clip_indexes": [1],
                "canonical_en": "Mira Iyer",
                "common_zh": "米拉·艾耶",
                "title_label": "米拉·艾耶",
                "confidence": "high",
                "evidence_urls": ["https://one.example/mira", "https://two.example/mira"],
            }]
        })
        lookup = [{
            "clip_indexes": [1],
            "results": [
                {
                    "url": "https://one.example/mira",
                    "title": "Mira Iyer 米拉·艾耶",
                    "snippet": "人物介绍",
                },
                {
                    "url": "https://two.example/mira",
                    "title": "米拉·艾耶专访",
                    "snippet": "Mira Iyer 谈市场",
                },
            ],
        }]

        titles.validate_guide_evidence(
            guide,
            lookup,
            [{"index": 1, "speaker": "Mira Iyer"}],
        )

        self.assertEqual(guide["entities"][0]["verified_source_count"], 2)
        self.assertTrue(titles.guide_item_is_verified(guide["entities"][0]))

    def test_entity_scope_must_match_the_original_clip_brief(self) -> None:
        guide = titles.parse_entity_guide({
            "entities": [{
                "source_text": "Person From Another Clip",
                "entity_type": "person",
                "clip_indexes": [1],
                "canonical_en": "Person From Another Clip",
                "common_zh": "跨片人物",
                "title_label": "跨片人物",
                "confidence": "high",
                "evidence_urls": ["https://example.com/person"],
            }]
        })
        lookup = [{
            "clip_indexes": [1],
            "results": [{
                "url": "https://example.com/person",
                "title": "跨片人物",
                "snippet": "跨片人物的正式介绍",
            }],
        }]

        titles.validate_guide_evidence(
            guide,
            lookup,
            [{"index": 1, "speaker": "Actual Speaker", "source_title": "Different story"}],
        )

        self.assertEqual(guide["entities"][0]["clip_indexes"], [])
        self.assertEqual(guide["entities"][0]["verified_source_count"], 0)

    def test_evidence_scope_is_reduced_to_clips_with_two_supporting_domains(self) -> None:
        guide = titles.parse_entity_guide({
            "terms": [{
                "source_term": "NIL",
                "clip_indexes": [1, 2],
                "preferred_zh": "NIL权益",
                "title_label": "NIL权益",
                "confidence": "high",
                "evidence_urls": ["https://one.example/nil", "https://two.example/nil"],
            }]
        })
        lookup = [{
            "clip_indexes": [1],
            "results": [
                {
                    "url": "https://one.example/nil",
                    "title": "NIL NIL权益",
                    "snippet": "college sports rights",
                },
                {
                    "url": "https://two.example/nil",
                    "title": "NIL权益",
                    "snippet": "NIL athlete rights",
                },
            ],
        }]

        titles.validate_guide_evidence(
            guide,
            lookup,
            [
                {"index": 1, "source_title": "College NIL rules"},
                {"index": 2, "source_title": "Profit was NIL"},
            ],
        )

        term = guide["terms"][0]
        self.assertEqual(term["clip_indexes"], [1])
        self.assertEqual(titles.apply_entity_replacements("Profit was NIL", guide, 2), "Profit was NIL")

    def test_formal_organization_name_normalizes_before_wording_guard(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    entity_type="organization",
                    source_text="China Investment Corporation",
                    canonical_en="China Investment Corporation",
                    official_zh="中国投资有限责任公司",
                    common_zh="中投公司",
                    title_label="中投公司",
                )
            ]
        }
        plan = {
            "clips": [{
                "title": "中国投资有限责任公司看AI",
                "title_lines": ["中国投资有限责任公司", "AI支出", "还会增加吗？"],
                "title_highlights": ["中国投资有限责任公司"],
            }]
        }

        titles.sanitize_plan_wording_entity_aware(plan, guide)

        self.assertEqual(plan["clips"][0]["title"], "中投公司看AI")
        self.assertEqual(plan["clips"][0]["title_lines"][0], "中投公司")

    def test_title_label_prefix_does_not_block_long_legal_name_replacement(self) -> None:
        entities = [
            self.verified_entity(
                entity_type="organization",
                source_text="China Life Insurance Company Limited",
                canonical_en="China Life Insurance Company Limited",
                official_zh="中国人寿保险股份有限公司",
                common_zh="中国人寿",
                title_label="中国人寿",
            ),
            self.verified_entity(
                entity_type="organization",
                source_text="Ping An Insurance (Group) Company of China",
                canonical_en="Ping An Insurance (Group) Company of China",
                official_zh="中国平安保险（集团）股份有限公司",
                common_zh="中国平安",
                title_label="中国平安",
            ),
            self.verified_entity(
                entity_type="organization",
                source_text="Allianz Group",
                canonical_en="Allianz Group",
                official_zh="安联保险集团",
                common_zh="安联保险",
                title_label="安联保险",
            ),
        ]
        guide = {"entities": entities}

        self.assertEqual(
            titles.apply_entity_replacements("中国人寿保险股份有限公司上调预期", guide, 1),
            "中国人寿上调预期",
        )
        self.assertEqual(
            titles.apply_entity_replacements("中国平安保险（集团）股份有限公司上调预期", guide, 1),
            "中国平安上调预期",
        )
        self.assertEqual(
            titles.apply_entity_replacements("安联保险集团关注养老", guide, 1),
            "安联保险关注养老",
        )

    def test_fallback_plan_keeps_verified_organization_name_consistent_across_fields(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    entity_type="organization",
                    source_text="China Life Insurance Company Limited",
                    canonical_en="China Life Insurance Company Limited",
                    official_zh="中国人寿保险股份有限公司",
                    common_zh="中国人寿",
                    title_label="中国人寿",
                )
            ]
        }
        plan = {"clips": [{
            "title": "中国人寿保险股份有限公司关注房价",
            "title_lines": ["中国人寿保险股份有限公司", "关注房价", "市场怎么走？"],
            "comment": "中国人寿保险股份有限公司给出判断",
            "subtitles": [{"zh": "中国人寿保险股份有限公司给出最新判断"}],
        }]}

        titles.apply_refinements(plan, {}, "test", [], guide)

        clip = plan["clips"][0]
        self.assertIn("中国人寿", clip["title"])
        self.assertNotIn("中国人寿保障", json.dumps(clip, ensure_ascii=False))
        self.assertNotIn("中国人寿保险股份有限公司", json.dumps(clip, ensure_ascii=False))
        self.assertEqual(plan["title_refine"]["status"], "planner_fallback")

    def test_fallback_without_search_evidence_preserves_obvious_legal_names(self) -> None:
        plan = {"clips": [{
            "title": "中国投资有限责任公司关注房价与投资机会",
            "title_lines": ["中国投资有限责任公司", "关注房价", "投资机会在哪？"],
            "title_highlights": ["中国投资有限责任公司", "房价", "投资机会"],
            "comment": "中国人寿保险股份有限公司也在关注房价",
            "subtitles": [{"zh": "中国平安保险（集团）股份有限公司给出判断"}],
        }]}

        titles.apply_refinements(
            plan,
            {},
            "test",
            [],
            {"entities": [], "terms": [], "clip_research": []},
        )

        clip_json = json.dumps(plan["clips"][0], ensure_ascii=False)
        self.assertIn("中国投资有限责任公司", clip_json)
        self.assertIn("中国人寿保险股份有限公司", clip_json)
        self.assertIn("中国平安保险（集团）股份有限公司", clip_json)
        self.assertNotIn("中国配置有限责任公司", clip_json)
        self.assertNotIn("中国人寿保障股份有限公司", clip_json)
        self.assertIn("地产", clip_json)
        self.assertIn("机会线索", clip_json)
        self.assertIn("中国投资有限责任公司", plan["clips"][0]["title_highlights"])

    def test_entity_protection_does_not_reverse_generic_wording(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    source_text="Example Asset Management",
                    canonical_en="Example Asset Management",
                    common_zh="示例投资",
                    title_label="示例投资",
                )
            ]
        }
        plan = {"clips": [{
            "title": "Example Asset Management：这只是示例配置方案",
            "title_lines": ["Example Asset Management", "示例配置方案", "还会增加吗？"],
            "title_highlights": ["Example Asset Management"],
        }]}

        titles.sanitize_plan_wording_entity_aware(plan, guide)

        clip = plan["clips"][0]
        self.assertIn("示例投资", clip["title"])
        self.assertIn("示例配置方案", clip["title"])
        self.assertEqual(clip["title_lines"][0], "示例投资")

    def test_four_character_chinese_org_alias_does_not_pollute_generic_copy(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    entity_type="organization",
                    source_text="China Investment Corporation",
                    canonical_en="China Investment Corporation",
                    common_zh="中投公司",
                    title_label="中投公司",
                    aliases=["中国投资"],
                )
            ]
        }

        self.assertEqual(
            titles.apply_entity_replacements("今年中国投资增长8%", guide, 1),
            "今年中国投资增长8%",
        )

    def test_short_chinese_org_source_text_is_not_a_safe_alias(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    entity_type="organization",
                    source_text="中国投资",
                    canonical_en="",
                    official_zh="",
                    common_zh="中投公司",
                    title_label="中投公司",
                )
            ]
        }

        self.assertEqual(
            titles.apply_entity_replacements("今年中国投资增长8%", guide, 1),
            "今年中国投资增长8%",
        )

    def test_replacement_chains_reach_a_stable_terminal_in_one_pass(self) -> None:
        guide = {
            "entities": [
                self.verified_entity(
                    entity_type="organization",
                    source_text="XYZ",
                    canonical_en="XYZ",
                    common_zh="终点机构",
                    title_label="终点机构",
                ),
                self.verified_entity(
                    entity_type="organization",
                    source_text="ABC",
                    canonical_en="ABC",
                    official_zh="",
                    common_zh="XYZ",
                    title_label="XYZ",
                ),
            ]
        }

        once = titles.apply_entity_replacements("ABC上调预期", guide, 1)
        twice = titles.apply_entity_replacements(once, guide, 1)

        self.assertEqual(once, "终点机构上调预期")
        self.assertEqual(twice, once)

    def test_verified_term_uses_contextual_title_label(self) -> None:
        guide = {
            "terms": [
                {
                    "source_term": "NIL",
                    "title_label": "NIL权益",
                    "avoid_zh": ["likeness权"],
                    "clip_indexes": [3],
                    "confidence": "high",
                    "verified_source_count": 2,
                    "evidence_urls": ["https://example.com/nil", "https://second.example/nil"],
                }
            ]
        }

        self.assertEqual(titles.apply_entity_replacements("NIL and likeness权", guide, 3), "NIL权益 and NIL权益")
        self.assertEqual(titles.apply_entity_replacements("NIL权怎么定价", guide, 3), "NIL权益怎么定价")

    def test_cover_line_never_hard_cuts_verified_name_or_number_unit(self) -> None:
        guide = {"entities": [self.verified_entity()]}
        clip = {"title": "Sports", "subtitles": []}

        name_line = titles.clean_cover_line("挪威球星 Erling Haaland", clip, guide, 1)
        number_line = titles.clean_cover_line("青少年体育支出五年涨46%", clip, guide, 1)

        self.assertEqual(name_line, "挪威球星 哈兰德")
        self.assertTrue(name_line.endswith("哈兰德"))
        self.assertTrue(number_line.endswith("46%"))

    def test_cover_line_removes_title_badge_before_punctuation_cleanup(self) -> None:
        guide = {"entities": [self.verified_entity()]}
        clip = {"title": "Sports", "subtitles": []}

        self.assertEqual(
            titles.clean_cover_line("标题：哈兰德五年增长46%", clip, guide, 1),
            "哈兰德五年增长46%",
        )


class PromptContractTests(unittest.TestCase):
    def test_candidate_prompt_operationalizes_emotion_polarity(self) -> None:
        briefs = [{"index": 1, "subtitles": []}]
        prompt = titles.candidate_user_prompt(briefs, "test", [], {"clip_research": [], "entities": []})

        self.assertIn("二极管法则", prompt)
        self.assertIn("exactly four candidates", prompt)
        self.assertIn("emotion_tension is at least 8/10", prompt)
        self.assertIn("words_vs_actions.supported is true", prompt)
        self.assertIn("china_advantage.supported is true", prompt)

    def test_surgical_prompt_separates_internal_labels_from_reader_copy(self) -> None:
        brief = {"index": 1, "current_title": "中国AI", "subtitles": []}
        current = {
            "title": "外资策略师罕见直言中国AI更便宜",
            "title_lines": ["外资策略师", "罕见直言", "中国AI更便宜"],
        }

        prompt = titles.surgical_repair_user_prompt(
            brief,
            {"entities": [], "clip_research": []},
            current,
            ["remove_internal_editorial_labels_from_reader_copy"],
        )

        self.assertIn("internal metadata", prompt)
        self.assertIn("directly show the subject", prompt)
        self.assertIn("do not use synonyms for the same editorial narration", prompt)

    def test_surgical_hook_repair_requires_a_concrete_question(self) -> None:
        brief = {
            "index": 2,
            "current_title": "大众车型砍半",
            "source_title": "Volkswagen cuts model lineup",
            "subtitles": [{"zh": "来自中国汽车企业的竞争正在加剧", "en": "Chinese competition is rising"}],
        }
        current = {
            "title": "大众砍车型一半成本谈判却未达预期",
            "title_lines": ["大众砍车型一半", "成本谈判", "却未达预期"],
        }

        prompt = titles.surgical_repair_user_prompt(
            brief,
            {"entities": [], "clip_research": []},
            current,
            ["make_the_emotional_reversal_visible_in_words"],
        )

        self.assertIn("must be a concrete consequence question ending in ？", prompt)
        self.assertIn("CHINA-RELATED REPAIR", prompt)
        self.assertIn("Do not return a declarative summary", prompt)

    def test_unverified_entities_are_excluded_from_downstream_research_prompt(self) -> None:
        verified = EntityReplacementTests.verified_entity()
        unverified = EntityReplacementTests.verified_entity(
            source_text="Wrong Name",
            canonical_en="Wrong Name",
            common_zh="错误译名",
            title_label="错误译名",
            evidence_urls=[],
            verified_source_count=9,
        )

        research = titles.research_for_indexes(
            {"entities": [verified, unverified], "terms": [], "clip_research": []},
            {1},
        )

        self.assertEqual([item["title_label"] for item in research["entities"]], ["哈兰德"])


class RefinementFlowTests(unittest.TestCase):
    @patch.object(titles, "repair_refinement_surgically", return_value=None)
    @patch.object(titles, "refine_batch")
    def test_one_rejected_clip_does_not_discard_successful_refinements(
        self,
        refine_batch,
        _repair,
    ) -> None:
        accepted = {
            "title": "成功标题",
            "title_lines": ["机构", "具体事实", "后续会怎样？"],
            "title_quality_audit": {"pass": True},
        }
        rejected = {
            "title": "失败标题",
            "title_lines": ["机械人名", "泛化主题", "前景如何？"],
            "title_quality_audit": {"pass": False, "fixes": ["replace_flat_summary_or_generic_question"]},
        }
        refine_batch.return_value = {1: accepted, 2: rejected}
        plan = {"clips": [{"title": "Planner 1"}, {"title": "Planner 2"}]}

        refinements = titles.refine_titles(
            plan,
            api_key="fake-key",
            style="test",
            batch_size=4,
            max_subtitles=24,
            public_lookup=[],
            entity_guide={"entities": [], "terms": [], "clip_research": []},
        )

        self.assertEqual(refinements, {1: accepted})

    @patch.object(titles, "repair_refinement_surgically", return_value=None)
    @patch.object(titles, "refine_batch")
    def test_api_failure_keeps_successes_from_earlier_batches(
        self,
        refine_batch,
        _repair,
    ) -> None:
        accepted = {
            "title": "成功标题",
            "title_lines": ["机构", "具体事实", "后续会怎样？"],
            "title_quality_audit": {"pass": True},
        }
        refine_batch.side_effect = [
            {1: accepted},
            TimeoutError("temporary API failure"),
            TimeoutError("temporary API failure"),
        ]
        plan = {"clips": [{"title": "Planner 1"}, {"title": "Planner 2"}]}

        refinements = titles.refine_titles(
            plan,
            api_key="fake-key",
            style="test",
            batch_size=1,
            max_subtitles=24,
            public_lookup=[],
            entity_guide={"entities": [], "terms": [], "clip_research": []},
        )

        self.assertEqual(refinements, {1: accepted})

    def test_apply_refinements_records_partial_status(self) -> None:
        plan = {
            "clips": [
                {"title": "Planner 1", "title_lines": ["Planner 1"]},
                {"title": "Planner 2", "title_lines": ["Planner 2"]},
            ]
        }
        accepted = {
            "title": "成功标题",
            "title_lines": ["机构", "具体事实", "后续会怎样？"],
            "title_highlights": ["具体事实"],
            "comment": "KC评论：具体事实值得看",
            "comment_highlights": ["具体事实"],
            "subtitle_comments": [],
            "title_quality_audit": {"pass": True},
        }

        titles.apply_refinements(
            plan,
            {1: accepted},
            "test",
            [],
            {"entities": [], "terms": [], "clip_research": []},
        )

        self.assertEqual(plan["title_refine"]["status"], "partial_refined")
        self.assertEqual(plan["title_refine"]["fallback_clip_indexes"], [2])

    def test_only_china_resonance_seven_can_be_accepted_after_repairs(self) -> None:
        refined = {
            "title_quality_audit": {
                "pass": False,
                "fixes": ["make_china_direction_constructive_or_positive"],
                "semantic_thresholds": {"china_resonance": 7},
            }
        }

        accepted = titles.accept_near_miss_after_repair(refined)

        self.assertTrue(accepted)
        self.assertTrue(refined["title_quality_audit"]["pass"])
        self.assertEqual(refined["title_quality_audit"]["pass_level"], "accepted_after_repair")

    def test_near_miss_does_not_override_a_missing_visible_hook(self) -> None:
        refined = {
            "title_quality_audit": {
                "pass": False,
                "fixes": [
                    "make_china_direction_constructive_or_positive",
                    "make_the_emotional_reversal_visible_in_words",
                ],
                "semantic_thresholds": {"china_resonance": 7},
            }
        }

        self.assertFalse(titles.accept_near_miss_after_repair(refined))

    @patch.object(titles, "ask_deepseek")
    def test_surgical_repair_changes_only_title_fields(self, ask_deepseek) -> None:
        ask_deepseek.return_value = {
            "clips": [
                {
                    "index": 1,
                    "angle_id": "china_advantage",
                    "emotion_pole": "民族自豪",
                    "viewer_reaction": "原来优势这么具体",
                    "evidence_basis": ["字幕称中国AI可靠且便宜"],
                    "title": "中国AI既便宜又可靠海外份额要反转？",
                    "title_lines": ["中国AI", "既便宜又可靠", "海外份额要反转？"],
                    "title_highlights": ["中国AI", "便宜", "海外份额"],
                    "runner_up_titles": ["中国AI性价比改写海外份额？"],
                    "editor_scores": passing_scores(),
                    "quality_check": passing_quality_check(),
                }
            ]
        }
        clip = {
            "title": "中国AI",
            "subtitles": [
                {
                    "index": 1,
                    "zh": "中国AI更可靠也更便宜",
                    "en": "Chinese AI is reliable and inexpensive",
                }
            ],
        }
        brief = titles.clip_brief({}, clip, 1, 24)
        current = {
            "title": "外资策略师罕见直言中国AI更便宜",
            "title_lines": ["外资策略师", "罕见直言", "中国AI更便宜"],
            "title_highlights": ["中国AI"],
            "angle_id": "outsider_candor",
            "emotion_pole": "终于有人说",
            "viewer_reaction": "终于有人说了",
            "evidence_basis": ["字幕称中国AI更便宜"],
            "runner_up_titles": [],
            "editor_scores": passing_scores(),
            "quality_check": passing_quality_check(),
            "comment": "KC评论：中国AI的成本优势开始转化为市场竞争力",
            "comment_highlights": ["成本优势"],
            "subtitle_comments": [
                {
                    "subtitle_index": 1,
                    "comment": "KC评论：便宜与可靠可以同时成立",
                    "comment_highlights": ["便宜与可靠"],
                }
            ],
            "formula_id": "legacy",
            "title_quality_audit": {
                "pass": False,
                "fixes": ["remove_internal_editorial_labels_from_reader_copy"],
            },
        }

        repaired = titles.repair_refinement_surgically(
            "test-key",
            brief,
            clip,
            {"entities": [], "clip_research": []},
            current,
        )

        self.assertIsNotNone(repaired)
        assert repaired is not None
        self.assertEqual(repaired["title"], "中国AI：既便宜又可靠，海外份额要反转？")
        self.assertEqual(repaired["comment"], current["comment"])
        self.assertEqual(repaired["subtitle_comments"], current["subtitle_comments"])
        self.assertTrue(repaired["title_quality_audit"]["pass"])

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
                    "angle_id": "china_advantage",
                    "emotion_pole": "民族自豪",
                    "viewer_reaction": "原来中国AI强在这里",
                    "evidence_basis": ["字幕明确称中国AI可靠且便宜"],
                    "title": "中国AI既便宜又可靠海外份额要反转？",
                    "title_lines": ["中国AI", "既便宜又可靠", "海外份额要反转？"],
                    "title_highlights": ["中国AI", "便宜", "海外份额"],
                    "runner_up_titles": ["中国AI性价比改写海外份额？"],
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
        self.assertEqual(refined[1]["angle_id"], "china_advantage")
        self.assertEqual(
            refined[1]["title"],
            "中国AI：既便宜又可靠，海外份额要反转？",
        )
        self.assertIn("candidate_raw_result", events[0])


if __name__ == "__main__":
    unittest.main()

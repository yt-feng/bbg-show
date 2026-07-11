#!/usr/bin/env python3
"""Refine rendered clip titles with DeepSeek for stronger short-video hooks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import html as html_lib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_speaker_full import clean_text, normalize_highlights, safe_zh  # noqa: E402
from plan_speaker_highlights import ask_deepseek  # noqa: E402
from trump_filter import remove_trump_clips_from_plan  # noqa: E402
from wording_guard import WORDING_GUARD_PROMPT, sanitize_plan_wording, sanitize_zh_wording  # noqa: E402


TITLE_LINE_LIMITS = (10, 12, 14)
TITLE_MAX_CHARS = 36
DEFAULT_BATCH_SIZE = 4
DEFAULT_MAX_SUBTITLES = 24
DEFAULT_LOOKUP_MAX_QUERIES = 12
DEFAULT_LOOKUP_RESULTS_PER_QUERY = 3
TITLE_QUALITY_MIN_SCORE = 78
TITLE_RESEARCH_VERSION = "emotion-polarity-v2"
TITLE_EMOTION_POLES = {
    "意外",
    "惊喜",
    "强疑惑",
    "终于有人说",
    "反差质疑",
    "民族自豪",
}
TITLE_ANGLE_IDS = {
    "surprise_reversal",
    "outsider_candor",
    "words_vs_actions",
    "china_advantage",
    "concrete_stakes",
    "authority_breaks_consensus",
}
TITLE_DATA_LESSONS = {
    "source": "KC桌面-视频号动态数据明细.csv, 114 rows, July 2026 local analysis",
    "winning_patterns": [
        "机构/人物权威 + 具体数字/资产 + 强问题，例如 高盛王逸：4-5万亿城市更新会拖住楼市吗",
        "大机构/名人 + 被逼/陷阱/真相/转向 等张力词，例如 野村辜朝明：日本央行被逼到墙角",
        "具体数据 + 政策动作疑问，例如 6月PMI释放积极信号，政府会加码吗",
        "反常识判断 + 下一步条件，例如 低估值可能是价值陷阱，需等待政策转向消费",
        "中国相关议题更容易获得点击，但表达要落在政策、需求、信心、估值、产业升级这些正向框架",
        "外资或海外权威给出对中国更积极的判断时，反差本身就是主标题，不要埋在摘要里",
        "让观众产生单一强反应：居然如此、终于有人说、他说的是真的吗、原来中国优势在这里",
        "大实话角度来自表达者身份与观点反差；言行反差角度必须有公开动作或披露支撑",
        "涉及中国比较优势时，优先把外国企业被迫调整、海外权威改口或中国优势被低估写成冲突",
    ],
    "avoid_patterns": [
        "过长的新闻句，尤其是 24 字以上还塞多个从句",
        "只有 策略/机会/影响/前瞻/市场/风险 这类泛词，没有具体对象或张力",
        "美国宏观泛标题如果没有美联储/鲍威尔等强锚点，点击明显弱",
        "纯描述式标题，例如 某市场短期风险与长期催化剂，缺少问题和冲突",
        "经济危机/金融危机/崩盘/崩溃 这类过硬负面财经词",
        "把事实换成疑问号但没有反差，例如 回报路径在哪、未来如何、能否实现",
        "陌生英文人名或机构全称占据第一行，却没有告诉观众这个人为什么值得听",
        "把 Bloomberg LP 等来源或公司法律后缀误当成采访对象",
        "没有公开证据却暗示嘉宾撒谎、操纵或言行不一",
    ],
}
TITLE_BIG_ANCHOR_RE = re.compile(
    r"(高盛|摩根士丹利|摩根大通|美联储|野村|中金|美银|花旗|贝莱德|桥水|富达|瀚亚|"
    r"CLSA|中银|马斯克|黄仁勋|鲍威尔|巴菲特|辜朝明|洪灏|邢自强|王逸|Kevin|"
    r"腾讯|阿里|英伟达|苹果|微软|OpenAI|ARK|木头姐|Cathie)",
    re.IGNORECASE,
)
TITLE_NUMBER_RE = re.compile(r"(\d|万亿|万|亿|%|美元|基点|BP|bp|PMI|CPI|PCE)", re.IGNORECASE)
TITLE_HOOK_RE = re.compile(
    r"(？|\?|吗|为何|为什么|怎么|会不会|能否|是否|真相|反转|意外|罕见|居然|竟然|"
    r"终于|敢说|说透|嘴上|手里|改口|认了|急了|逼急|被逼|砍半|砍一半|墙角|陷阱|托底|"
    r"转向|拐点|分歧|重估|低估|藏不住|另一回事|两回事|另一码事|关键|底牌|底|"
    r"加码|掩盖|冰火两重天|低估值)"
)
TITLE_FLAT_RE = re.compile(
    r"(市场机会|市场影响|策略前瞻|成焦点|长期催化剂|短期压力|值得关注|"
    r"回报路径在哪|前景如何|未来如何|有待观察|仍不明朗|面临挑战|带来机遇|"
    r"释放信号|成本谈判未达预期|关键分叉在哪|(?:股东)?回报(?:仍)?(?:存疑|待解|成疑)|"
    r"(?:盈利|前景|效果)(?:仍)?(?:存疑|待解|成疑)|"
    r"(?:有望|预计|可能)(?:扩大|增长|提升|改善))(?:？|\?)?$"
)
TITLE_SOURCE_LABEL_RE = re.compile(
    r"^(?:彭博(?:社|有限合伙企业|有限责任公司)?|Bloomberg(?:\s+L\.?P\.?)?)"
    r"(?:对话|采访|报道|解读)?$",
    re.IGNORECASE,
)
TITLE_EDITORIAL_META_RE = re.compile(
    r"(外资策略师|外资首席|海外策略师|海外专家|西方策略师|外媒点破|外媒直言|西方机构承认|"
    r"外资(?:罕见|终于|也)(?:直言|承认|看好|改口|说透)?|罕见直言|终于有人说|"
    r"说了句大实话|大实话|只有外资[^，。？！?]{0,8}敢说)"
)
TITLE_POSITION_CHANGE_RE = re.compile(r"(改口|口风突变|态度反转|突然看多|突然看空)")
TITLE_WORDS_ACTIONS_RE = re.compile(r"(嘴上.+手里|说一套.+做一套|言行不一|押反了|押注相反)")
TITLE_INCOMPLETE_TENSION_RE = re.compile(r"(逼急|逼到|迫使|倒逼)$")
CHINA_CONTEXT_RE = re.compile(
    r"(中国|中资|中企|中概|内地|国内|人民币|楼市|房价|房地产|地产|A股|港股|"
    r"China|Chinese|Hong Kong|renminbi|yuan|property|housing|real estate)",
    re.IGNORECASE,
)
CHINA_NEGATIVE_FRAMING_REPLACEMENTS = (
    (re.compile(r"(?:外资|资金|资本)(?:正)?(?:集体)?逃离(中国资产|中国市场|A股|港股|中概股?)"), r"\1再定价"),
    (re.compile(r"(?:唱衰|看空)(中国(?:资产|市场|经济)?)"), r"\1信心修复"),
    (re.compile(r"(?:崩溃|崩盘|塌了|垮了)"), "承压"),
    (re.compile(r"(?:完了|没救了?|不行了?|药丸)"), "承压"),
    (re.compile(r"(?:被抛弃|被放弃|遭抛弃)"), "被重新定价"),
    (re.compile(r"(?:外资逃离|资金逃离|集体逃离)"), "资金再配置"),
    (re.compile(r"(?:惨败|失败|输麻了)"), "遇挑战"),
    (re.compile(r"(?:很惨|太惨|惨了)"), "承压"),
)
PUBLIC_SEARCH_URL = "https://lite.duckduckgo.com/lite/"
PUBLIC_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TITLE_BADGE_PATTERNS = [
    re.compile(r"[【\[]\s*(?:彭博社?|Bloomberg)?\s*独家\s*[】\]]\s*", re.IGNORECASE),
    re.compile(r"^(?:彭博社?|Bloomberg)?\s*独家\s*[：:｜|\-、\s]*", re.IGNORECASE),
    re.compile(r"\bBloomberg\s+Exclusive\b\s*[：:｜|\-、\s]*", re.IGNORECASE),
    re.compile(r"彭博社?独家\s*[：:｜|\-、\s]*"),
]
GENERIC_ENTITY_STOPWORDS = {
    "Bloomberg",
    "China",
    "Chinese",
    "Asia",
    "Asian",
    "Market",
    "Markets",
    "Video",
    "Videos",
    "News",
    "Show",
    "Top",
    "The",
    "A",
    "An",
    "And",
    "Of",
    "For",
    "To",
    "In",
    "On",
    "With",
    "From",
    "Says",
    "Said",
    "Chief",
    "Economist",
    "Strategist",
    "Managing",
    "Partner",
    "Officer",
    "CIO",
    "CEO",
}
ENTITY_CANDIDATE_REJECT_WORDS = {
    "Says",
    "Said",
    "Sees",
    "See",
    "Tied",
    "Weak",
    "Strong",
    "May",
    "Might",
    "Have",
    "Has",
    "Had",
    "Bottomed",
    "Bottoming",
    "Drops",
    "Drop",
    "Rises",
    "Rise",
    "Falls",
    "Fall",
    "After",
    "Before",
    "Why",
    "How",
    "What",
    "When",
}
COMMON_TRADITIONAL_TO_SIMPLIFIED = str.maketrans({
    "灝": "灏",
    "蓮": "莲",
    "華": "华",
    "資": "资",
    "產": "产",
    "國": "国",
    "際": "际",
    "證": "证",
    "券": "券",
    "經": "经",
    "濟": "济",
    "學": "学",
    "師": "师",
    "總": "总",
    "監": "监",
    "顧": "顾",
    "問": "问",
    "寶": "宝",
    "豐": "丰",
    "銀": "银",
    "聯": "联",
    "儲": "储",
    "壽": "寿",
    "萬": "万",
    "億": "亿",
    "臺": "台",
    "台": "台",
})


def strip_title_badges(text: str) -> str:
    value = clean_text(text)
    for pattern in TITLE_BADGE_PATTERNS:
        value = pattern.sub("", value)
    return clean_text(value.strip(" ：:｜|-、[]【】"))


def compact_text(text: str, max_chars: int) -> str:
    value = safe_zh(to_simplified_common(strip_title_badges(text)))
    value = sanitize_zh_wording(value, for_title=True)
    value = value.replace("：", "").replace(":", "")
    value = re.sub(r"^(?:标题|观点|看点|结论)\s*[：:]\s*", "", value)
    value = value.strip(" ，,。；;、｜|-")
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip(" ，,。；;、｜|-")


def compact_title(text: str) -> str:
    value = safe_zh(to_simplified_common(strip_title_badges(text)))
    value = sanitize_zh_wording(value, for_title=True)
    value = re.sub(r"\s+", "", value)
    value = value.strip(" ，,。；;、｜|-")
    if len(value) <= TITLE_MAX_CHARS:
        return value
    return value[:TITLE_MAX_CHARS].rstrip(" ，,。；;、｜|-")


def to_simplified_common(text: str) -> str:
    return text.translate(COMMON_TRADITIONAL_TO_SIMPLIFIED)


def clip_duration(clip: dict[str, Any]) -> float:
    try:
        return round(float(clip.get("end", 0)) - float(clip.get("start", 0)), 1)
    except (TypeError, ValueError):
        return 0.0


def is_china_related_clip(clip: dict[str, Any]) -> bool:
    fields = [
        str(clip.get("title", "")),
        str(clip.get("speaker_context", "")),
        str(clip.get("source_title", "")),
    ]
    for subtitle in clip.get("subtitles", [])[:8]:
        if isinstance(subtitle, dict):
            fields.append(str(subtitle.get("zh", "")))
            fields.append(str(subtitle.get("en", "")))
    return bool(CHINA_CONTEXT_RE.search(" ".join(fields)))


def clip_wording_context(clip: dict[str, Any]) -> str:
    fields = [
        str(clip.get("title", "")),
        str(clip.get("speaker", "")),
        str(clip.get("speaker_context", "")),
        str(clip.get("source_title", "")),
        " ".join(str(item) for item in clip.get("title_lines", []) if item),
    ]
    for subtitle in clip.get("subtitles", [])[:12]:
        if isinstance(subtitle, dict):
            fields.append(str(subtitle.get("zh", "")))
            fields.append(str(subtitle.get("en", "")))
    return " ".join(fields)


def china_safe_title_text(text: str, clip: dict[str, Any]) -> str:
    if not text:
        return text
    value = text
    if is_china_related_clip(clip):
        for pattern, replacement in CHINA_NEGATIVE_FRAMING_REPLACEMENTS:
            value = pattern.sub(replacement, value)
    return sanitize_zh_wording(value, context=clip_wording_context(clip), for_title=True)


def title_quality_audit(
    refined: dict[str, Any],
    clip: dict[str, Any],
    research_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lines = refined.get("title_lines", [])
    if not isinstance(lines, list):
        lines = []
    joined = "".join(str(line) for line in lines)
    title = str(refined.get("title", "")) or joined
    text = f"{title}{joined}"

    quality_check = refined.get("quality_check")
    if not isinstance(quality_check, dict):
        quality_check = {}
    editor_scores = refined.get("editor_scores")
    if not isinstance(editor_scores, dict):
        editor_scores = {}

    def score_value(key: str) -> int:
        try:
            return max(0, min(10, int(float(editor_scores.get(key, 0)))))
        except (TypeError, ValueError):
            return 0

    emotion_tension = score_value("emotion_tension")
    novelty = score_value("novelty")
    specificity = score_value("specificity")
    curiosity_gap = score_value("curiosity_gap")
    factual_fidelity = score_value("factual_fidelity")
    china_resonance = score_value("china_resonance")
    angle_id = clean_text(str(refined.get("angle_id", "")))
    emotion_pole = clean_text(str(refined.get("emotion_pole", "")))

    score = 20
    strengths: list[str] = []
    fixes: list[str] = []

    has_anchor = bool(TITLE_BIG_ANCHOR_RE.search(text)) or bool(quality_check.get("has_recognizable_anchor"))
    if has_anchor:
        score += 12
        strengths.append("has_authority_anchor")
    else:
        fixes.append("add_recognizable_actor_or_role")

    has_specific_fact = bool(TITLE_NUMBER_RE.search(text)) or bool(quality_check.get("has_specific_fact"))
    if has_specific_fact:
        score += 12
        strengths.append("has_specific_fact")
    else:
        fixes.append("add_specific_number_action_comparison_or_consequence")

    has_visible_hook = bool(TITLE_HOOK_RE.search(text))
    if has_visible_hook:
        score += 10
        strengths.append("has_hook_tension")
    else:
        fixes.append("make_the_emotional_reversal_visible_in_words")

    if angle_id in TITLE_ANGLE_IDS:
        score += 6
        strengths.append(f"angle:{angle_id}")
    else:
        fixes.append("choose_supported_angle_id")

    if emotion_pole in TITLE_EMOTION_POLES:
        score += 8
        strengths.append(f"emotion:{emotion_pole}")
    else:
        fixes.append("choose_one_emotion_pole")

    if emotion_tension >= 8:
        score += 8
        strengths.append("high_emotion_tension")
    else:
        fixes.append("raise_emotion_tension_to_8")

    if novelty >= 7:
        score += 7
        strengths.append("has_novelty")
    else:
        fixes.append("show_baseline_then_reversal")

    if specificity >= 7:
        score += 5
    else:
        fixes.append("replace_generic_language_with_a_concrete_fact")

    if curiosity_gap >= 7:
        score += 7
        strengths.append("has_curiosity_gap")
    else:
        fixes.append("create_a_small_answerable_information_gap")

    if factual_fidelity >= 9 and bool(quality_check.get("is_factually_supported")):
        score += 10
        strengths.append("factually_grounded")
    else:
        fixes.append("restore_transcript_and_research_fidelity")

    if is_china_related_clip(clip):
        if china_resonance >= 8 and bool(quality_check.get("passes_china_frame")):
            score += 8
            strengths.append("china_positive_resonance")
        else:
            score -= 12
            fixes.append("make_china_direction_constructive_or_positive")

    if any(len(str(line)) > TITLE_LINE_LIMITS[min(idx, 2)] for idx, line in enumerate(lines[:3])):
        score -= 8
        fixes.append("tighten_display_lines")

    if len(title) > TITLE_MAX_CHARS:
        score -= 10
        fixes.append("shorten_full_title")

    is_flat = bool(TITLE_FLAT_RE.search(title) or TITLE_FLAT_RE.search(joined))
    if is_flat:
        score -= 22
        fixes.append("replace_flat_summary_or_generic_question")

    has_source_actor = any(TITLE_SOURCE_LABEL_RE.fullmatch(str(line).strip()) for line in lines[:2])
    if has_source_actor:
        score -= 30
        fixes.append("replace_publisher_source_with_the_real_actor_or_topic")

    has_editorial_meta = bool(TITLE_EDITORIAL_META_RE.search(text))
    if has_editorial_meta:
        score -= 30
        fixes.append("remove_internal_editorial_labels_from_reader_copy")

    research_item = research_item if isinstance(research_item, dict) else {}
    position_change = research_item.get("position_change")
    if not isinstance(position_change, dict):
        position_change = {}
    try:
        position_source_count = int(position_change.get("independent_source_count", 0) or 0)
    except (TypeError, ValueError):
        position_source_count = 0
    position_change_supported = bool(position_change.get("supported")) and position_source_count >= 2
    has_unsupported_position_change = bool(TITLE_POSITION_CHANGE_RE.search(text)) and not position_change_supported
    if has_unsupported_position_change:
        score -= 35
        fixes.append("remove_unverified_position_change_claim")

    words_vs_actions = research_item.get("words_vs_actions")
    if not isinstance(words_vs_actions, dict):
        words_vs_actions = {}
    try:
        action_source_count = int(words_vs_actions.get("independent_source_count", 0) or 0)
    except (TypeError, ValueError):
        action_source_count = 0
    words_vs_actions_supported = bool(words_vs_actions.get("supported")) and action_source_count >= 2
    has_unsupported_words_actions = bool(TITLE_WORDS_ACTIONS_RE.search(text)) and not words_vs_actions_supported
    if has_unsupported_words_actions:
        score -= 35
        fixes.append("remove_unverified_words_versus_actions_claim")

    has_incomplete_tension = any(
        TITLE_INCOMPLETE_TENSION_RE.search(str(line).strip())
        for line in lines
    )
    if has_incomplete_tension:
        score -= 20
        fixes.append("complete_the_tension_phrase_with_its_subject_or_object")

    if re.search(r"(今日热点|核心观点速览|值得关注|速看|必看|震惊)", text):
        score -= 16
        fixes.append("remove_generic_clickbait_or_filler")

    guarded = sanitize_zh_wording(text, context=clip_wording_context(clip), for_title=True)
    if guarded != text:
        score -= 12
        fixes.append("wording_guard_changed_title")

    all_quality_checks = all(
        bool(quality_check.get(key))
        for key in (
            "has_recognizable_anchor",
            "has_specific_fact",
            "has_single_emotion_pole",
            "has_novelty_or_surprise",
            "has_curiosity_gap",
            "is_factually_supported",
            "passes_china_frame",
            "passes_wording_guard",
        )
    )
    if not all_quality_checks:
        fixes.append("complete_all_model_quality_checks")

    semantic_pass = (
        angle_id in TITLE_ANGLE_IDS
        and emotion_pole in TITLE_EMOTION_POLES
        and emotion_tension >= 8
        and novelty >= 7
        and specificity >= 7
        and curiosity_gap >= 7
        and factual_fidelity >= 9
        and all_quality_checks
        and has_anchor
        and has_specific_fact
        and has_visible_hook
        and not is_flat
        and not has_source_actor
        and not has_editorial_meta
        and not has_unsupported_position_change
        and not has_unsupported_words_actions
        and not has_incomplete_tension
    )
    if is_china_related_clip(clip):
        semantic_pass = semantic_pass and china_resonance >= 8

    score = max(0, min(100, score))
    return {
        "score": score,
        "pass": score >= TITLE_QUALITY_MIN_SCORE and semantic_pass,
        "strengths": strengths,
        "fixes": list(dict.fromkeys(fixes)),
        "semantic_thresholds": {
            "emotion_tension": emotion_tension,
            "novelty": novelty,
            "specificity": specificity,
            "curiosity_gap": curiosity_gap,
            "factual_fidelity": factual_fidelity,
            "china_resonance": china_resonance,
        },
    }


def subtitle_sample(clip: dict[str, Any], max_subtitles: int) -> list[dict[str, Any]]:
    sample: list[dict[str, Any]] = []
    for fallback_index, subtitle in enumerate(clip.get("subtitles", [])[:max_subtitles], start=1):
        zh = clean_text(str(subtitle.get("zh", "")))
        en = clean_text(str(subtitle.get("en", "")))
        if zh or en:
            try:
                subtitle_index = int(subtitle.get("index", fallback_index))
            except (TypeError, ValueError):
                subtitle_index = fallback_index
            item: dict[str, Any] = {
                "index": subtitle_index,
                "zh": zh[:180],
                "en": en[:240],
            }
            for key in ("relative_start", "relative_end"):
                if key in subtitle:
                    try:
                        item[key] = round(float(subtitle[key]), 1)
                    except (TypeError, ValueError):
                        pass
            sample.append(item)
    return sample


def all_clip_briefs(plan: dict[str, Any], clips: list[dict[str, Any]], max_subtitles: int) -> list[dict[str, Any]]:
    return [
        clip_brief(plan, clip, index, max_subtitles)
        for index, clip in enumerate(clips, start=1)
    ]


def clip_brief(
    plan: dict[str, Any],
    clip: dict[str, Any],
    index: int,
    max_subtitles: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "duration_seconds": clip_duration(clip),
        "speaker": clean_text(str(clip.get("speaker") or plan.get("speaker") or "")),
        "speaker_context": clean_text(str(clip.get("speaker_context") or plan.get("speaker_context") or "")),
        "source_title": clean_text(str(clip.get("source_title") or plan.get("source_title") or "")),
        "show_date": clean_text(str(plan.get("show_date", ""))),
        "current_title": clean_text(str(clip.get("title", ""))),
        "current_title_lines": [clean_text(str(item)) for item in clip.get("title_lines", []) if item],
        "current_comment": clean_text(str(clip.get("comment", ""))),
        "subtitles": subtitle_sample(clip, max_subtitles),
    }


def has_ascii_letter(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text))


def entity_candidates_from_text(text: str) -> list[str]:
    value = clean_text(text)
    if not value or not has_ascii_letter(value):
        return []

    patterns = [
        r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,}|&|and|of|the|[A-Z][a-z]+\.?)){0,5}",
        r"\b[A-Z]{2,}(?:\s+[A-Z]{2,}){0,4}\b",
    ]
    candidates: list[str] = []
    for pattern in patterns:
        for match in re.finditer(pattern, value):
            candidate = clean_text(match.group(0).strip(" -:|,.;()[]"))
            if len(candidate) < 2:
                continue
            words = [word for word in re.split(r"\s+", candidate) if word]
            if not words:
                continue
            if all(word in GENERIC_ENTITY_STOPWORDS for word in words):
                continue
            if any(word in ENTITY_CANDIDATE_REJECT_WORDS for word in words):
                continue
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def add_query(queries: list[str], seen: set[str], query: str, max_queries: int) -> None:
    query = clean_text(query)
    if not query or query in seen or len(queries) >= max_queries:
        return
    seen.add(query)
    queries.append(query)


def compact_research_briefs(briefs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for brief in briefs:
        claims: list[str] = []
        for subtitle in brief.get("subtitles", [])[:10]:
            if not isinstance(subtitle, dict):
                continue
            text = clean_text(str(subtitle.get("en") or subtitle.get("zh") or ""))
            if text:
                claims.append(text[:220])
        compact.append({
            "index": brief.get("index"),
            "speaker": brief.get("speaker", ""),
            "speaker_context": brief.get("speaker_context", ""),
            "source_title": brief.get("source_title", ""),
            "current_title": brief.get("current_title", ""),
            "source_claims": claims,
        })
    return compact


def research_query_system_prompt() -> str:
    return (
        "You plan public-web searches for a Chinese short-video title desk. Return strict JSON only. "
        "The search results will be used to verify names and find a factual contrast, not to invent a story. "
        "Write concise search-engine queries in Chinese or English using exact entities and claims."
    )


def research_query_user_prompt(briefs: list[dict[str, Any]], max_queries: int) -> str:
    return f"""Plan at most {max_queries} public web searches for these clips:

{json.dumps(compact_research_briefs(briefs), ensure_ascii=False, indent=2)}

Return JSON:
{{
  "queries": [
    {{
      "query": "search engine query",
      "purpose": "entity_name|consensus|speaker_stance|public_position|china_comparison",
      "clip_indexes": [1],
      "why": "what exact fact this query should verify"
    }}
  ]
}}

Research priorities, in order:
1. Verify established Chinese names and exact institutions for non-Chinese people and organizations.
2. Establish the public baseline or consensus needed to tell whether the source claim is genuinely unexpected.
3. Verify whether a foreign institution or overseas expert is making an unusually positive or candid China-related claim.
4. Only when the source names a company, institution, or public figure and there is a concrete reason to suspect a mismatch, search official disclosures, holdings, business actions, forecasts, or prior public statements. Do not assume a mismatch.
5. Verify concrete China comparative advantages when the transcript makes that comparison.

Rules:
- Every query must contain an exact person, institution, company, product, statistic, or quoted claim. Never search vague phrases such as "is this surprising".
- Use two independently phrased queries for a possible words-versus-actions angle; otherwise that angle cannot be used.
- Use two independently phrased queries for a possible claim that the same speaker or institution changed position. No prior search result is not evidence of a change.
- Prefer official pages, filings, established financial media, company announcements, and exact bilingual mentions.
- Do not search sensitive geopolitical or military topics.
- Do not create queries merely to confirm the current title. Search the underlying claim and comparison.
- Spend queries on the clips with the strongest possible surprise, outsider-candor, China-advantage, or verified words-versus-actions angle.
"""


def parse_research_queries(
    result: dict[str, Any],
    *,
    max_queries: int,
    valid_indexes: set[int],
) -> list[dict[str, Any]]:
    raw_queries = result.get("queries")
    if not isinstance(raw_queries, list):
        return []

    planned: list[dict[str, Any]] = []
    seen: set[str] = set()
    allowed_purposes = {
        "entity_name",
        "consensus",
        "speaker_stance",
        "public_position",
        "china_comparison",
    }
    for raw in raw_queries:
        if isinstance(raw, str):
            raw = {"query": raw}
        if not isinstance(raw, dict):
            continue
        query = clean_text(str(raw.get("query", "")))[:220]
        key = query.casefold()
        if len(query) < 4 or key in seen:
            continue
        purpose = clean_text(str(raw.get("purpose", "consensus")))
        if purpose not in allowed_purposes:
            purpose = "consensus"
        raw_indexes = raw.get("clip_indexes")
        if not isinstance(raw_indexes, list):
            raw_indexes = []
        indexes: list[int] = []
        for value in raw_indexes:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if index in valid_indexes and index not in indexes:
                indexes.append(index)
        planned.append({
            "query": query,
            "purpose": purpose,
            "clip_indexes": indexes,
            "why": clean_text(str(raw.get("why", "")))[:240],
        })
        seen.add(key)
        if len(planned) >= max_queries:
            break
    return planned


def public_lookup_queries(briefs: list[dict[str, Any]], max_queries: int) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    for brief in briefs:
        speaker = clean_text(str(brief.get("speaker", "")))
        context = clean_text(str(brief.get("speaker_context", "")))

        if has_ascii_letter(speaker) and has_ascii_letter(context):
            add_query(queries, seen, f"{speaker} {context} Chinese name official", max_queries)
        if has_ascii_letter(speaker):
            add_query(queries, seen, f"{speaker} Chinese name finance", max_queries)
        if len(queries) >= max_queries:
            return queries

    for brief in briefs:
        speaker = clean_text(str(brief.get("speaker", "")))
        context = clean_text(str(brief.get("speaker_context", "")))
        source_title = clean_text(str(brief.get("source_title", "")))
        source_fields = [speaker, context, source_title]

        for candidate in entity_candidates_from_text(" ".join(source_fields)):
            add_query(queries, seen, f"{candidate} 中文名 官方", max_queries)
            if len(queries) >= max_queries:
                return queries

        if has_ascii_letter(source_title):
            title_query = re.sub(
                r"\b(?:Bloomberg|The China Show|Top Videos?|KC桌面|Video)\b",
                " ",
                source_title,
                flags=re.IGNORECASE,
            )
            title_query = clean_text(title_query)
            if title_query:
                add_query(queries, seen, f"{title_query[:160]} 中文 名称", max_queries)

        if len(queries) >= max_queries:
            break
    return queries


def plan_public_research_queries(
    api_key: str,
    briefs: list[dict[str, Any]],
    max_queries: int,
) -> list[dict[str, Any]]:
    valid_indexes = {
        int(brief["index"])
        for brief in briefs
        if isinstance(brief.get("index"), int)
    }
    try:
        result = ask_deepseek(
            api_key,
            research_query_system_prompt(),
            research_query_user_prompt(briefs, max_queries),
            temperature=0.1,
        )
        planned = parse_research_queries(
            result,
            max_queries=max_queries,
            valid_indexes=valid_indexes,
        )
    except SystemExit as exc:
        print(f"Research query planning failed; using entity lookup fallback: {exc}", flush=True)
        planned = []

    seen = {str(item["query"]).casefold() for item in planned}
    for query in public_lookup_queries(briefs, max_queries):
        if len(planned) >= max_queries:
            break
        if query.casefold() in seen:
            continue
        planned.append({
            "query": query,
            "purpose": "entity_name",
            "clip_indexes": [],
            "why": "Fallback query for established Chinese entity names.",
        })
        seen.add(query.casefold())
    return planned


def html_to_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    return clean_text(html_lib.unescape(value))


def parse_public_search_results(page: str, max_results: int) -> list[dict[str, str]]:
    pattern = re.compile(
        r"<a[^>]+class=['\"][^'\"]*result-link[^'\"]*['\"][^>]*>(?P<title>.*?)</a>"
        r".*?<td[^>]+class=['\"][^'\"]*result-snippet[^'\"]*['\"][^>]*>(?P<snippet>.*?)</td>"
        r".*?<span[^>]+class=['\"][^'\"]*link-text[^'\"]*['\"][^>]*>(?P<url>.*?)</span>",
        re.IGNORECASE | re.DOTALL,
    )
    results: list[dict[str, str]] = []
    for match in pattern.finditer(page):
        title = html_to_text(match.group("title"))
        snippet = html_to_text(match.group("snippet"))
        url = html_to_text(match.group("url"))
        if not title and not snippet:
            continue
        results.append({
            "title": title[:160],
            "snippet": snippet[:420],
            "url": url[:240],
        })
        if len(results) >= max_results:
            break
    return results


def search_public_web(query: str, max_results: int) -> list[dict[str, str]]:
    url = PUBLIC_SEARCH_URL + "?" + urlencode({"q": query})
    req = Request(url, headers={"User-Agent": PUBLIC_SEARCH_USER_AGENT})
    with urlopen(req, timeout=20) as resp:
        page = resp.read(700_000).decode("utf-8", "replace")
    return parse_public_search_results(page, max_results)


def public_entity_lookup(
    query_plans: list[dict[str, Any]],
    *,
    results_per_query: int,
) -> list[dict[str, Any]]:
    lookups: list[dict[str, Any]] = []
    if not query_plans or results_per_query < 1:
        return lookups

    def fetch(query_plan: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, str]]]:
        query = clean_text(str(query_plan.get("query", "")))
        if not query:
            return query_plan, []
        return query_plan, search_public_web(query, results_per_query)

    found_by_index: dict[int, dict[str, Any]] = {}
    worker_count = min(3, len(query_plans))
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {
            pool.submit(fetch, query_plan): index
            for index, query_plan in enumerate(query_plans)
        }
        for future in as_completed(futures):
            index = futures[future]
            query_plan = query_plans[index]
            query = clean_text(str(query_plan.get("query", "")))
            if not query:
                continue
            try:
                _, results = future.result()
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                print(f"Public lookup failed for {query!r}: {exc}", flush=True)
                continue
            if not results:
                print(f"Public lookup returned no results for {query!r}", flush=True)
                continue
            print(f"Public lookup: {query!r} -> {len(results)} result(s)", flush=True)
            found_by_index[index] = {
                "query": query,
                "purpose": query_plan.get("purpose", "consensus"),
                "clip_indexes": query_plan.get("clip_indexes", []),
                "why": query_plan.get("why", ""),
                "results": results,
            }

    for index in sorted(found_by_index):
        lookups.append(found_by_index[index])
    return lookups


def entity_guide_system_prompt() -> str:
    return (
        "You are a bilingual financial-news fact checker and editorial researcher. Return strict JSON only. "
        "Identify entities, verify established Simplified Chinese names, and build a per-clip evidence card "
        "for surprising or emotionally strong titles. Separate transcript facts from public context. "
        "A search snippet is a lead, not automatic proof. Never invent a translation, consensus, holding, "
        "business action, prior statement, or contradiction."
    )


def entity_guide_user_prompt(briefs: list[dict[str, Any]], public_lookup: list[dict[str, Any]]) -> str:
    return f"""Build a Chinese entity translation guide for these finance/news clips.

Source clip metadata:
{json.dumps(briefs, ensure_ascii=False, indent=2)}

Public lookup snippets:
{json.dumps(public_lookup, ensure_ascii=False, indent=2)}

Return JSON:
{{
  "entities": [
    {{
      "source_text": "English or original entity",
      "entity_type": "person|organization|company|place|event|other",
      "preferred_en": "canonical English name if available",
      "preferred_zh": "established Simplified Chinese name",
      "aliases": ["wrong or alternate Chinese/English mentions to replace"],
      "confidence": "high|medium|low",
      "evidence": "short reason from public snippets"
    }}
  ],
  "clip_research": [
    {{
      "index": 1,
      "source_claim": "the strongest claim explicitly present in this clip",
      "public_baseline": "what the public context normally expects, or empty if unverified",
      "surprise_gap": "why source_claim conflicts with public_baseline, or empty",
      "speaker_relation": "foreign_institution|foreign_expert|insider|company_executive|analyst|media_source|other",
      "supported_angles": ["surprise_reversal|outsider_candor|words_vs_actions|china_advantage|concrete_stakes|authority_breaks_consensus"],
      "words_vs_actions": {{
        "supported": false,
        "spoken_position": "",
        "public_action": "",
        "independent_source_count": 0,
        "evidence": []
      }},
      "position_change": {{
        "supported": false,
        "prior_position": "",
        "current_position": "",
        "independent_source_count": 0,
        "evidence": []
      }},
      "china_advantage": {{
        "supported": false,
        "comparison": "",
        "evidence": []
      }},
      "evidence": [
        {{"kind": "transcript|public", "query": "", "url": "", "fact": "short fact"}}
      ],
      "forbidden_claims": ["tempting claims that the evidence does not support"],
      "confidence": "high|medium|low"
    }}
  ],
  "notes": ["optional notes"]
}}

Rules:
- This is generic. Do not rely on a fixed list of names.
- Prefer official company pages, major financial media, Wikipedia/Wikidata-style summaries, and exact bilingual snippets.
- If public snippets show Traditional Chinese, convert to Simplified Chinese in preferred_zh.
- If there is not enough evidence for a Chinese name, set confidence to low and leave preferred_zh empty or use the original English name.
- Include likely mistranslations from current titles in aliases only when the public evidence supports a better name.
- Keep aliases short; they are used for string replacement.
- Build exactly one clip_research item for every input clip index.
- source_claim must come from that clip's subtitles. Do not import a claim from another clip in the batch.
- public_baseline and surprise_gap require relevant public snippets. Leave them empty when search returned no useful context.
- outsider_candor is supported only when an identifiable foreign institution/expert makes a clear, unusually direct claim.
- words_vs_actions is supported only when at least two independent public results, including an official disclosure or exact prior statement when available, show a concrete mismatch. A general impression is not enough.
- position_change is supported only when an exact earlier public position and the current position show a real change, backed by at least two independent public results. Search-result absence, no prior mention, or a vague reputation is not evidence of a change.
- Never label a person or institution dishonest. Record the exact spoken position and exact public action so the title editor can use a factual question.
- china_advantage is supported only when the transcript or public evidence makes a concrete comparison favorable to China, Chinese companies, Chinese talent, Chinese technology, Chinese consumers, or Chinese policy capacity.
- Do not treat Bloomberg, Bloomberg LP, the show host, or the publisher as the opinion-holding actor unless the clip is explicitly an editorial opinion piece.
- Use query and URL fields from Public lookup snippets so downstream title claims remain auditable.
"""


def parse_entity_guide(result: dict[str, Any]) -> dict[str, Any]:
    raw_entities = result.get("entities")
    if not isinstance(raw_entities, list):
        raw_entities = []

    entities: list[dict[str, Any]] = []
    for raw in raw_entities:
        if not isinstance(raw, dict):
            continue
        preferred_zh = compact_text(str(raw.get("preferred_zh", "")), 24)
        preferred_en = clean_text(str(raw.get("preferred_en", "")))[:80]
        source_text = clean_text(str(raw.get("source_text", "")))[:80]
        if not preferred_zh and not preferred_en:
            continue
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        clean_aliases: list[str] = []
        for alias in aliases:
            value = clean_text(to_simplified_common(str(alias)))
            if value and value not in clean_aliases and value != preferred_zh:
                clean_aliases.append(value[:40])
        entities.append({
            "source_text": source_text,
            "entity_type": clean_text(str(raw.get("entity_type", "other")))[:24] or "other",
            "preferred_en": preferred_en,
            "preferred_zh": preferred_zh,
            "aliases": clean_aliases[:8],
            "confidence": clean_text(str(raw.get("confidence", "low")))[:12] or "low",
            "evidence": clean_text(str(raw.get("evidence", "")))[:260],
        })

    raw_clip_research = result.get("clip_research")
    if not isinstance(raw_clip_research, list):
        raw_clip_research = []
    clip_research: list[dict[str, Any]] = []
    seen_indexes: set[int] = set()
    for raw in raw_clip_research:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw.get("index", 0))
        except (TypeError, ValueError):
            continue
        if index < 1 or index in seen_indexes:
            continue
        supported_angles = raw.get("supported_angles")
        if not isinstance(supported_angles, list):
            supported_angles = []
        clean_angles = [
            clean_text(str(angle))
            for angle in supported_angles
            if clean_text(str(angle)) in TITLE_ANGLE_IDS
        ]
        words_vs_actions = raw.get("words_vs_actions")
        if not isinstance(words_vs_actions, dict):
            words_vs_actions = {}
        position_change = raw.get("position_change")
        if not isinstance(position_change, dict):
            position_change = {}
        china_advantage = raw.get("china_advantage")
        if not isinstance(china_advantage, dict):
            china_advantage = {}
        evidence = raw.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        forbidden_claims = raw.get("forbidden_claims")
        if not isinstance(forbidden_claims, list):
            forbidden_claims = []
        try:
            independent_source_count = int(words_vs_actions.get("independent_source_count", 0) or 0)
        except (TypeError, ValueError):
            independent_source_count = 0
        try:
            position_source_count = int(position_change.get("independent_source_count", 0) or 0)
        except (TypeError, ValueError):
            position_source_count = 0
        clip_research.append({
            "index": index,
            "source_claim": clean_text(str(raw.get("source_claim", "")))[:360],
            "public_baseline": clean_text(str(raw.get("public_baseline", "")))[:360],
            "surprise_gap": clean_text(str(raw.get("surprise_gap", "")))[:360],
            "speaker_relation": clean_text(str(raw.get("speaker_relation", "other")))[:40] or "other",
            "supported_angles": list(dict.fromkeys(clean_angles)),
            "words_vs_actions": {
                "supported": bool(words_vs_actions.get("supported", False)),
                "spoken_position": clean_text(str(words_vs_actions.get("spoken_position", "")))[:280],
                "public_action": clean_text(str(words_vs_actions.get("public_action", "")))[:280],
                "independent_source_count": max(0, min(9, independent_source_count)),
                "evidence": words_vs_actions.get("evidence", [])[:6]
                if isinstance(words_vs_actions.get("evidence"), list)
                else [],
            },
            "position_change": {
                "supported": bool(position_change.get("supported", False)),
                "prior_position": clean_text(str(position_change.get("prior_position", "")))[:280],
                "current_position": clean_text(str(position_change.get("current_position", "")))[:280],
                "independent_source_count": max(0, min(9, position_source_count)),
                "evidence": position_change.get("evidence", [])[:6]
                if isinstance(position_change.get("evidence"), list)
                else [],
            },
            "china_advantage": {
                "supported": bool(china_advantage.get("supported", False)),
                "comparison": clean_text(str(china_advantage.get("comparison", "")))[:280],
                "evidence": china_advantage.get("evidence", [])[:6]
                if isinstance(china_advantage.get("evidence"), list)
                else [],
            },
            "evidence": evidence[:10],
            "forbidden_claims": [clean_text(str(item))[:240] for item in forbidden_claims if clean_text(str(item))][:8],
            "confidence": clean_text(str(raw.get("confidence", "low")))[:12] or "low",
        })
        seen_indexes.add(index)

    notes = result.get("notes")
    if not isinstance(notes, list):
        notes = []
    return {
        "research_version": TITLE_RESEARCH_VERSION,
        "entities": entities,
        "clip_research": clip_research,
        "notes": [clean_text(str(note))[:180] for note in notes if clean_text(str(note))][:8],
    }


def build_entity_translation_guide(
    api_key: str,
    briefs: list[dict[str, Any]],
    public_lookup: list[dict[str, Any]],
) -> dict[str, Any]:
    print("Building entity and editorial research guide with DeepSeek", flush=True)
    result = ask_deepseek(
        api_key,
        entity_guide_system_prompt(),
        entity_guide_user_prompt(compact_research_briefs(briefs), public_lookup),
        temperature=0.1,
    )
    guide = parse_entity_guide(result)
    existing_indexes = {
        int(item.get("index", 0))
        for item in guide.get("clip_research", [])
        if isinstance(item, dict) and str(item.get("index", "")).isdigit()
    }
    for brief in briefs:
        try:
            index = int(brief.get("index", 0))
        except (TypeError, ValueError):
            continue
        if index < 1 or index in existing_indexes:
            continue
        source_claim = clean_text(str(brief.get("current_title", "")))
        if not source_claim:
            for subtitle in brief.get("subtitles", []):
                if isinstance(subtitle, dict):
                    source_claim = clean_text(str(subtitle.get("zh") or subtitle.get("en") or ""))
                if source_claim:
                    break
        guide["clip_research"].append({
            "index": index,
            "source_claim": source_claim[:360],
            "public_baseline": "",
            "surprise_gap": "",
            "speaker_relation": "other",
            "supported_angles": ["concrete_stakes"],
            "words_vs_actions": {
                "supported": False,
                "spoken_position": "",
                "public_action": "",
                "independent_source_count": 0,
                "evidence": [],
            },
            "position_change": {
                "supported": False,
                "prior_position": "",
                "current_position": "",
                "independent_source_count": 0,
                "evidence": [],
            },
            "china_advantage": {"supported": False, "comparison": "", "evidence": []},
            "evidence": [{"kind": "transcript", "query": "", "url": "", "fact": source_claim[:300]}],
            "forbidden_claims": ["No public baseline was verified for this clip."],
            "confidence": "low",
        })
    guide["clip_research"].sort(key=lambda item: int(item.get("index", 0) or 0))
    return guide


def entity_replacement_pairs(entity_guide: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for entity in entity_guide.get("entities", []):
        if not isinstance(entity, dict):
            continue
        preferred = clean_text(str(entity.get("preferred_zh", "")))
        if not preferred:
            continue
        aliases = entity.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        for alias in aliases:
            alias_text = clean_text(str(alias))
            if alias_text and alias_text != preferred:
                pairs.append((alias_text, preferred))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def apply_entity_replacements(text: str, entity_guide: dict[str, Any]) -> str:
    value = to_simplified_common(text)
    for alias, preferred in entity_replacement_pairs(entity_guide):
        if preferred.startswith(alias):
            suffix = preferred[len(alias):]
            if not suffix:
                continue
            value = re.sub(
                re.escape(alias) + r"(?!" + re.escape(suffix) + r")",
                lambda _: preferred,
                value,
            )
        else:
            value = value.replace(alias, preferred)
    return value


def research_for_indexes(entity_guide: dict[str, Any], indexes: set[int]) -> dict[str, Any]:
    clip_research: list[dict[str, Any]] = []
    for item in entity_guide.get("clip_research", []):
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index", 0))
        except (TypeError, ValueError):
            continue
        if index in indexes:
            clip_research.append(item)
    return {
        "research_version": entity_guide.get("research_version", TITLE_RESEARCH_VERSION),
        "entities": entity_guide.get("entities", []),
        "clip_research": clip_research,
        "notes": entity_guide.get("notes", []),
    }


def research_item_for_index(entity_guide: dict[str, Any], index: int) -> dict[str, Any]:
    for item in entity_guide.get("clip_research", []):
        if not isinstance(item, dict):
            continue
        try:
            item_index = int(item.get("index", 0))
        except (TypeError, ValueError):
            continue
        if item_index == index:
            return item
    return {}


def lookups_for_indexes(public_lookup: list[dict[str, Any]], indexes: set[int]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for lookup in public_lookup:
        raw_indexes = lookup.get("clip_indexes")
        if not isinstance(raw_indexes, list) or not raw_indexes:
            selected.append(lookup)
            continue
        lookup_indexes: set[int] = set()
        for value in raw_indexes:
            try:
                lookup_indexes.add(int(value))
            except (TypeError, ValueError):
                continue
        if lookup_indexes & indexes:
            selected.append(lookup)
    return selected


def candidate_system_prompt() -> str:
    return (
        "You are a Chinese viral-title angle architect. Return strict JSON only. "
        "Do not write a safe summary. Generate competing, fact-grounded emotional angles for each clip. "
        "Each candidate must make a viewer feel one strong reaction instead of mild interest. "
        "Use the research guide as a hard boundary: unsupported surprise, dishonesty, holdings, public actions, "
        "and national comparisons are forbidden. "
        + WORDING_GUARD_PROMPT
    )


def candidate_user_prompt(
    briefs: list[dict[str, Any]],
    style: str,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
) -> str:
    indexes = {int(brief["index"]) for brief in briefs}
    return f"""Create four genuinely different title candidates for every clip.

Input clips:
{json.dumps(briefs, ensure_ascii=False, indent=2)}

Public lookup evidence:
{json.dumps(lookups_for_indexes(public_lookup, indexes), ensure_ascii=False, indent=2)}

Verified research guide:
{json.dumps(research_for_indexes(entity_guide, indexes), ensure_ascii=False, indent=2)}

Return JSON:
{{
  "clips": [
    {{
      "index": 1,
      "fact_anchor": "the exact transcript fact every candidate preserves",
      "candidates": [
        {{
          "candidate_id": "c1",
          "angle_id": "surprise_reversal|outsider_candor|words_vs_actions|china_advantage|concrete_stakes|authority_breaks_consensus",
          "emotion_pole": "意外|惊喜|强疑惑|终于有人说|反差质疑|民族自豪",
          "viewer_reaction": "居然是这样|终于有人说了|他说的是真的吗|原来中国强在这里",
          "title": "完整中文标题",
          "title_lines": ["第一行", "第二行", "第三行"],
          "factual_mode": "statement|question",
          "evidence_basis": ["transcript fact or public evidence reference"],
          "scores": {{
            "emotion_tension": 0,
            "novelty": 0,
            "specificity": 0,
            "curiosity_gap": 0,
            "factual_fidelity": 0,
            "china_resonance": 0
          }},
          "fatal_issue": ""
        }}
      ]
    }}
  ]
}}

Operating rule: 二极管法则
- A candidate is usable only if one emotion pole is unmistakable and emotion_tension is at least 8/10.
- The six permitted poles are: unexpected reversal, pleasant surprise, strong doubt, finally-someone-said-it candor, verified words-versus-actions contrast, and fact-based pride in a Chinese comparative advantage.
- Do not split the emotion across several mild ideas. Pick one pole and push it to the factual limit.
- A question mark is not emotion. "前景如何", "回报路径在哪", "能否实现", "面临哪些挑战", "回报存疑", and "释放什么信号" score at most 4 unless they contain a concrete contradiction or consequence.

Angle rules:
- surprise_reversal: state the familiar baseline, then expose the source claim that reverses it. Do not turn a baseline contrast into "改口" unless research.position_change.supported is true with at least two independent sources.
- outsider_candor: use only when a foreign institution/expert says something unusually direct. Keep the identity contrast in evidence_basis and viewer_reaction, then realize it in final copy by placing the specific surprising claim against its consequence. Never literally output 外资策略师、外资首席、罕见直言、外媒点破、终于有人说、只有外资敢说、大实话, or 西方机构承认.
- words_vs_actions: use only when research.words_vs_actions.supported is true and independent_source_count is at least 2. Show the two exact sides in question form. Never directly call anyone a liar.
- china_advantage: use only when research.china_advantage.supported is true. Put the concrete Chinese advantage or the foreign competitor's forced response in the foreground. The emotional direction must favor China.
- authority_breaks_consensus: the authority must be recognizable to a China audience, or be labeled by a meaningful role. Do not lead with an obscure romanized name.
- concrete_stakes: expose a specific number, forced choice, consequence, or who gains/loses. Generic importance is not a stake.

Writing rules:
- Style: {style}
- Produce exactly four candidates per clip and use at least three different angle_id values when the evidence permits.
- Each candidate has exactly three short title lines. Think cover blocks, not a newspaper sentence.
- Ideal line lengths are 3-10, 4-12, and 5-14 Chinese characters. Full title is ideally 10-24 Chinese characters.
- The title must reveal enough familiar context to create a small information gap, then withhold the explanation supplied by the video.
- Prefer concrete verbs and consequences: 逼急、藏不住、说透、砍半. Use only when facts support them. "改口" requires research.position_change.supported with two independent sources; words-versus-actions phrases require research.words_vs_actions.supported with two independent sources.
- Do not mechanically copy examples or proper nouns from instructions. Derive every noun, number, actor, and comparison from this clip and its evidence card.
- angle_id, emotion_pole, and viewer_reaction are private editorial metadata. Never copy their wording into title or title_lines.
- Final cover structure should be [specific subject] / [unexpected fact or comparison] / [concrete unresolved consequence], not [speaker category] / [editorial label] / [topic].
- If a person or institution is genuinely recognizable, use its verified proper name. If it is not recognizable, omit the identity and lead with the subject and fact; do not replace it with generic labels such as 外资策略师 or 海外专家.
- Bloomberg/Bloomberg LP/彭博社 is a source label, not the expert or actor. Company legal suffixes do not belong in a title.
- For China-related clips, never attack China or imply national decline. Favor confidence, competence, policy room, industrial strength, cost advantage, talent, resilience, and competitors being forced to respond.
- If the clip is negative about China and no constructive factual angle exists, use a neutral mechanism question instead of nationalist or doom framing.
- Forbidden title words: 资产管理、投资、股票、基金、理财、保险、投顾、荐股、买入、卖出.
- Forbidden hard-negative wording: 经济危机、金融危机、债务危机、危机、崩盘、崩溃、完了、没救、惨了.
- Do not use sensitive geopolitical or military subjects, emojis, markdown, quotation marks, hashtags, or numbered prefixes.
- If evidence cannot support four dramatic factual claims, vary the framing and use strong questions; never fabricate the missing drama.
"""


def system_prompt() -> str:
    return (
        "You are the chief Chinese short-video title editor and tournament judge. Return strict JSON only. "
        "Select or rewrite the strongest candidate for thumb-stop and completion. A neutral summary is a failure. "
        "At the same time, transcript fidelity and verified research boundaries are absolute. "
        "Use high-arousal surprise, curiosity, candor, contrast, or China-positive pride without empty sensationalism. "
        "Never directly accuse a person or institution of lying. "
        + WORDING_GUARD_PROMPT
    )


def user_prompt(
    briefs: list[dict[str, Any]],
    style: str,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
    candidate_result: dict[str, Any],
    repair_context: dict[str, Any] | None = None,
) -> str:
    indexes = {int(brief["index"]) for brief in briefs}
    repair_block = (
        "\nPrevious finals failed the code audit. Repair every listed problem and return a stronger final:\n"
        + json.dumps(repair_context, ensure_ascii=False, indent=2)
        if repair_context
        else ""
    )
    return f"""Run a title tournament for these Chinese vertical short-video clips.

Input clips:
{json.dumps(briefs, ensure_ascii=False, indent=2)}

Verified research guide:
{json.dumps(research_for_indexes(entity_guide, indexes), ensure_ascii=False, indent=2)}

Candidate pool from the angle architect:
{json.dumps(candidate_result, ensure_ascii=False, indent=2)}
{repair_block}

Return JSON:
{{
  "clips": [
    {{
      "index": 1,
      "formula_id": "legacy-compatible short label",
      "angle_id": "surprise_reversal|outsider_candor|words_vs_actions|china_advantage|concrete_stakes|authority_breaks_consensus",
      "emotion_pole": "意外|惊喜|强疑惑|终于有人说|反差质疑|民族自豪",
      "viewer_reaction": "the one immediate audience reaction",
      "evidence_basis": ["specific transcript or research fact"],
      "title": "完整中文标题",
      "title_lines": ["第一行", "第二行", "第三行"],
      "title_highlights": ["关键词1", "关键词2"],
      "runner_up_titles": ["候选标题1", "候选标题2"],
      "editor_scores": {{
        "emotion_tension": 0,
        "novelty": 0,
        "specificity": 0,
        "curiosity_gap": 0,
        "factual_fidelity": 0,
        "china_resonance": 0
      }},
      "quality_check": {{
        "has_recognizable_anchor": true,
        "has_specific_fact": true,
        "has_single_emotion_pole": true,
        "has_novelty_or_surprise": true,
        "has_curiosity_gap": true,
        "is_factually_supported": true,
        "passes_china_frame": true,
        "passes_wording_guard": true
      }},
      "comment": "KC评论：一句话解释为什么值得关注",
      "comment_highlights": ["关键词1"],
      "subtitle_comments": [
        {{
          "subtitle_index": 1,
          "comment": "KC评论：基于这句字幕的短评",
          "comment_highlights": ["关键词1"]
        }}
      ]
    }}
  ]
}}

Tournament procedure, perform in this order:
1. Evidence veto: reject candidates that use a name, number, consensus, public action, comparison, or accusation not supported by that clip's transcript/research card.
2. China direction veto: for China-related clips, reject any candidate whose emotional target is China, Chinese people, Chinese companies, or China's future. Comparative pride is allowed only when supported.
3. Neutrality veto: reject flat summaries and generic questions. A question mark alone does not create tension.
4. Meta-copy veto: reject titles that expose the editor's reasoning with labels such as 外资策略师、外资首席、海外专家、西方策略师、罕见直言、外媒点破、终于有人说、只有外资敢说、大实话、 or 西方机构承认. The viewer should feel the effect from the facts, not be told how to feel.
5. Audience test: complete exactly one sentence: "看完标题，观众第一反应是____". If the answer is merely "我知道发生了什么", reject it.
6. Score survivors from 0-10. Final requires emotion_tension >= 8, novelty >= 7, specificity >= 7, curiosity_gap >= 7, factual_fidelity >= 9. For China clips, china_resonance >= 8.
7. Select the winner or combine only two candidates. If no candidate passes, write one new title, then score it honestly.
8. Before returning, verify every quality_check field. If any is false, rewrite once. Never return a known failure.

Editorial interpretation:
- 二极管 means one high-activation pole, not random anger: 意外, 惊喜, 强疑惑, 终于有人说, 有证据的言行反差, or 民族自豪.
- 新鲜感 requires a baseline plus a reversal. "外资谈中国" is ordinary; "外资在普遍悲观时罕见给出积极判断" is a usable reversal only if public_baseline supports it.
- 大实话 comes from who said what and why that identity makes the candor surprising. Do not paste "大实话" onto a routine forecast.
- 大实话、终于有人说、反差质疑 and 民族自豪 describe the intended viewer response internally. Do not print those labels or their editorial scaffolding in the title. Express them through the specific fact, comparison, and consequence.
- The desired effect of a possible falsehood angle is factual suspicion, not a verdict. Only when words_vs_actions is verified may you use a question such as "嘴上X，手里却Y？".
- "改口" means the same actor demonstrably held a different prior position. It requires research.position_change.supported and at least two independent sources. Never infer a change from search-result absence, lack of prior coverage, or a vague reputation.
- 民族自豪 comes from a concrete comparison: cost, technology, talent, supply chain, demand, speed, resilience, or a foreign competitor's forced adjustment. Do not use empty slogans.
- Familiar authority helps. Use a verified proper name only when the audience is likely to recognize it. Otherwise lead with the concrete subject; generic editorial identities such as 外资策略师、海外专家 or 西方策略师 are not final copy.
- Bloomberg/Bloomberg LP/彭博社 is normally the source, never the guest. Do not output 彭博有限合伙企业.

Title and display rules:
- Style: {style}
- Use exactly three non-empty title lines, each a punchy cover block. Ideal lengths: 3-10, 4-12, 5-14 Chinese characters.
- Full title should be short and sharp, ideally 10-24 Chinese characters.
- Keep only one core conflict. Do not pack background, claim, caveat, and conclusion into one line.
- Highlights must be exact substrings of joined title_lines and emphasize the actor, number, reversal, China advantage, or tension.
- Do not use flat endings such as 回报路径在哪、回报存疑、前景如何、未来如何、有待观察、仍不明朗、面临挑战、带来机遇、释放信号、关键分叉在哪、有望扩大. Turn the caveat into a concrete unresolved action or contradiction.
- Every line must be a complete Chinese phrase. Do not leave a transitive tension verb such as 逼急、逼到、迫使 or 倒逼 without its subject or object.
- Do not use source badges, emojis, markdown, quotation marks, hashtags, or numbering.
- Title fields must not contain 资产管理、投资、股票、基金、理财、保险、投顾、荐股、买入、卖出.
- Never use 经济危机、金融危机、债务危机、危机、崩盘、崩溃、完了、没救、惨了.
- Do not mention sensitive geopolitical or military topics.

KC comment rules:
- comment starts with KC评论： and adds context, consequence, or the next fact to watch in 16-34 Chinese characters. Do not repeat the title.
- subtitle_comments contains one item for every input subtitle index, grounded in that exact subtitle rather than a generic clip slogan.
- Each subtitle comment starts with KC评论： and is ideally 10-24 Chinese characters after the prefix, with a complete ending.
- Adjacent subtitle comments must vary between evidence, implication, tension, consequence, and context.
- All highlight strings must be exact substrings of their corresponding title/comment.

Data lessons:
{json.dumps(TITLE_DATA_LESSONS, ensure_ascii=False, indent=2)}
"""


def parse_items(result: dict[str, Any]) -> dict[int, dict[str, Any]]:
    raw_items = result.get("clips")
    if not isinstance(raw_items, list):
        raw_items = result.get("items")
    if not isinstance(raw_items, list):
        return {}

    items: dict[int, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        try:
            index = int(raw["index"])
        except (KeyError, TypeError, ValueError):
            continue
        items[index] = raw
    return items


def fallback_lines(clip: dict[str, Any], entity_guide: dict[str, Any] | None = None) -> list[str]:
    lines = clip.get("title_lines")
    if isinstance(lines, list):
        cleaned = [
            compact_text(
                china_safe_title_text(apply_entity_replacements(str(line), entity_guide or {}), clip),
                TITLE_LINE_LIMITS[min(idx, 2)],
            )
            for idx, line in enumerate(lines[:3])
        ]
        if len(cleaned) == 3 and all(cleaned):
            return cleaned

    speaker = compact_text(
        china_safe_title_text(apply_entity_replacements(str(clip.get("speaker", "")), entity_guide or {}), clip),
        TITLE_LINE_LIMITS[0],
    )
    title = compact_title(
        china_safe_title_text(apply_entity_replacements(str(clip.get("title", "")), entity_guide or {}), clip)
    )
    if "：" in title:
        left, right = title.split("：", 1)
    elif ":" in title:
        left, right = title.split(":", 1)
    else:
        left, right = speaker or "Bloomberg", title
    return [
        compact_text(china_safe_title_text(left or speaker or "Bloomberg", clip), TITLE_LINE_LIMITS[0]),
        compact_text(china_safe_title_text(right or title or "市场焦点", clip), TITLE_LINE_LIMITS[1]),
        "关键转折来了",
    ]


def normalize_comment_payload(
    raw_comment: Any,
    raw_highlights: Any,
    fallback_comment: str,
    entity_guide: dict[str, Any],
    *,
    body_max_chars: int,
) -> tuple[str, list[str]]:
    comment = apply_entity_replacements(str(raw_comment or ""), entity_guide)
    comment = safe_zh(to_simplified_common(strip_title_badges(comment)))
    comment = re.sub(r"\s+", "", comment)
    comment = comment.strip(" ，,。；;、｜|-")
    if comment and not comment.startswith("KC评论："):
        comment = "KC评论：" + comment.removeprefix("KC评论:").removeprefix("KC点评：").removeprefix("点评：")
    if not comment:
        comment = fallback_comment
    if comment and not comment.startswith("KC评论："):
        comment = "KC评论：" + comment.removeprefix("KC评论:").removeprefix("KC点评：").removeprefix("点评：")

    prefix = "KC评论："
    if len(comment) > len(prefix) + body_max_chars:
        body = comment[len(prefix):] if comment.startswith(prefix) else comment
        comment = prefix + truncate_comment_body(body, body_max_chars)

    if isinstance(raw_highlights, list):
        raw_highlights = [apply_entity_replacements(str(item), entity_guide) for item in raw_highlights]
    highlights = normalize_highlights(raw_highlights, comment, limit=2)
    if not highlights:
        body = comment.removeprefix("KC评论：")
        if body:
            highlights = [body[: min(8, len(body))]]
    return comment, highlights[:2]


def truncate_comment_body(body: str, max_chars: int) -> str:
    body = body.strip(" ，,。；;、｜|-")
    if len(body) <= max_chars:
        return body
    clipped = body[:max_chars].rstrip(" ，,。；;、｜|-")
    min_keep = max(8, max_chars // 2)
    for marker in ("，", "；", "。", "、", ",", ";"):
        idx = clipped.rfind(marker)
        if idx >= min_keep:
            return clipped[:idx].rstrip(" ，,。；;、｜|-")
    return clipped


def normalize_comment(raw: dict[str, Any], clip: dict[str, Any], entity_guide: dict[str, Any]) -> tuple[str, list[str]]:
    title = compact_title(str(clip.get("title", "")))
    fallback = f"KC评论：这条线索值得继续跟踪" if not title else f"KC评论：{title[:14]}背后有新信号"
    return normalize_comment_payload(
        raw.get("comment", ""),
        raw.get("comment_highlights"),
        fallback,
        entity_guide,
        body_max_chars=34,
    )


def subtitle_index(subtitle: dict[str, Any], fallback: int) -> int:
    try:
        return int(subtitle.get("index", fallback))
    except (TypeError, ValueError):
        return fallback


def fallback_subtitle_comment(clip: dict[str, Any], subtitle: dict[str, Any]) -> str:
    raw_highlights = subtitle.get("zh_highlights")
    if isinstance(raw_highlights, list):
        for raw in raw_highlights:
            key = compact_text(str(raw), 10)
            if key:
                return f"KC评论：盯住{key}这个信号"

    zh = clean_text(str(subtitle.get("zh_filtered") or subtitle.get("zh") or ""))
    if zh:
        phrase = compact_text(zh, 12)
        if phrase:
            return f"KC评论：{phrase}是关键信号"

    title = compact_title(str(clip.get("title", "")))
    return f"KC评论：{title[:12]}这里有新信号" if title else "KC评论：这句是判断关键"


def normalize_subtitle_comments(
    raw: dict[str, Any],
    clip: dict[str, Any],
    entity_guide: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_items = raw.get("subtitle_comments")
    if not isinstance(raw_items, list):
        return []

    subtitles = clip.get("subtitles", [])
    if not isinstance(subtitles, list) or not subtitles:
        return []

    by_index: dict[int, dict[str, Any]] = {}
    for fallback, subtitle in enumerate(subtitles, start=1):
        if isinstance(subtitle, dict):
            by_index[subtitle_index(subtitle, fallback)] = subtitle

    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("subtitle_index", item.get("index", 0)))
        except (TypeError, ValueError):
            continue
        if idx in seen or idx not in by_index:
            continue
        comment, highlights = normalize_comment_payload(
            item.get("comment", ""),
            item.get("comment_highlights", item.get("highlights")),
            fallback_subtitle_comment(clip, by_index[idx]),
            entity_guide,
            body_max_chars=34,
        )
        normalized.append({
            "subtitle_index": idx,
            "comment": comment,
            "comment_highlights": highlights,
        })
        seen.add(idx)

    normalized.sort(key=lambda item: int(item["subtitle_index"]))
    return normalized


def normalize_editor_scores(raw_scores: Any) -> dict[str, int]:
    if not isinstance(raw_scores, dict):
        raw_scores = {}
    normalized: dict[str, int] = {}
    for key in (
        "emotion_tension",
        "novelty",
        "specificity",
        "curiosity_gap",
        "factual_fidelity",
        "china_resonance",
    ):
        try:
            value = int(float(raw_scores.get(key, 0)))
        except (TypeError, ValueError):
            value = 0
        normalized[key] = max(0, min(10, value))
    return normalized


def normalize_item(
    raw: dict[str, Any],
    clip: dict[str, Any],
    entity_guide: dict[str, Any],
    research_item: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    fallback = fallback_lines(clip, entity_guide)
    raw_lines = raw.get("title_lines")
    if not isinstance(raw_lines, list) or len(raw_lines) != 3:
        raw_lines = fallback

    lines = [
        compact_text(
            china_safe_title_text(apply_entity_replacements(str(raw_lines[idx]), entity_guide), clip),
            TITLE_LINE_LIMITS[idx],
        ) or fallback[idx]
        for idx in range(3)
    ]
    if not all(lines):
        return None

    # Keep filenames/descriptions aligned with the three cover blocks selected by the editor.
    title = compact_title(f"{lines[0]}：{lines[1]}，{lines[2]}")

    joined = "".join(lines)
    raw_highlights = raw.get("title_highlights")
    if isinstance(raw_highlights, list):
        raw_highlights = [apply_entity_replacements(str(item), entity_guide) for item in raw_highlights]
    highlights = normalize_highlights(raw_highlights, joined, limit=3)
    if not highlights:
        highlights = [line for line in lines[1:] if line][:2]
    comment, comment_highlights = normalize_comment(raw, clip, entity_guide)

    raw_evidence = raw.get("evidence_basis")
    if not isinstance(raw_evidence, list):
        raw_evidence = []
    evidence_basis = [
        clean_text(str(item))[:300]
        for item in raw_evidence
        if clean_text(str(item))
    ][:8]
    raw_runners = raw.get("runner_up_titles")
    if not isinstance(raw_runners, list):
        raw_runners = []
    runner_up_titles = [
        compact_title(china_safe_title_text(apply_entity_replacements(str(item), entity_guide), clip))
        for item in raw_runners
        if clean_text(str(item))
    ][:3]
    quality_check = raw.get("quality_check") if isinstance(raw.get("quality_check"), dict) else {}

    refined = {
        "title": title,
        "title_lines": lines,
        "title_highlights": highlights[:3],
        "comment": comment,
        "comment_highlights": comment_highlights,
        "subtitle_comments": normalize_subtitle_comments(raw, clip, entity_guide),
        "formula_id": clean_text(str(raw.get("formula_id", "")))[:48],
        "angle_id": clean_text(str(raw.get("angle_id", "")))[:48],
        "emotion_pole": clean_text(str(raw.get("emotion_pole", "")))[:24],
        "viewer_reaction": clean_text(str(raw.get("viewer_reaction", "")))[:80],
        "evidence_basis": evidence_basis,
        "runner_up_titles": [item for item in runner_up_titles if item],
        "editor_scores": normalize_editor_scores(raw.get("editor_scores")),
        "quality_check": quality_check,
    }
    refined["title_quality_audit"] = title_quality_audit(refined, clip, research_item)
    return refined


def candidate_result_for_indexes(candidate_result: dict[str, Any], indexes: set[int]) -> dict[str, Any]:
    raw_clips = candidate_result.get("clips")
    if not isinstance(raw_clips, list):
        return candidate_result
    selected: list[dict[str, Any]] = []
    for item in raw_clips:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("index", 0))
        except (TypeError, ValueError):
            continue
        if index in indexes:
            selected.append(item)
    return {"clips": selected}


def refine_batch(
    api_key: str,
    plan: dict[str, Any],
    clips: list[dict[str, Any]],
    indexes: list[int],
    *,
    style: str,
    max_subtitles: int,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
    log_events: list[dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    briefs = [
        clip_brief(plan, clips[index - 1], index, max_subtitles)
        for index in indexes
    ]
    candidate_system_text = candidate_system_prompt()
    candidate_prompt_text = candidate_user_prompt(briefs, style, public_lookup, entity_guide)
    candidate_result = ask_deepseek(
        api_key,
        candidate_system_text,
        candidate_prompt_text,
        temperature=0.65,
    )

    system_text = system_prompt()
    prompt_text = user_prompt(briefs, style, public_lookup, entity_guide, candidate_result)
    result = ask_deepseek(
        api_key,
        system_text,
        prompt_text,
        temperature=0.25,
    )
    parsed = parse_items(result)

    normalized: dict[int, dict[str, Any]] = {}
    for index in indexes:
        item = parsed.get(index)
        if not item:
            continue
        refined = normalize_item(
            item,
            clips[index - 1],
            entity_guide,
            research_item_for_index(entity_guide, index),
        )
        if refined:
            normalized[index] = refined

    repair_events: list[dict[str, Any]] = []
    failed_indexes = [
        index
        for index in indexes
        if index not in normalized or not normalized[index]["title_quality_audit"].get("pass")
    ]
    if failed_indexes:
        print(
            "Repairing title quality for clip(s) " + ",".join(map(str, failed_indexes)),
            flush=True,
        )
        failed_briefs = [brief for brief in briefs if int(brief["index"]) in failed_indexes]
        repair_context = {
            "previous_finals": {
                str(index): normalized.get(index, {})
                for index in failed_indexes
            },
            "required_fixes": {
                str(index): (
                    normalized[index].get("title_quality_audit", {}).get("fixes", [])
                    if index in normalized
                    else ["return_a_complete_valid_item"]
                )
                for index in failed_indexes
            },
        }
        repair_candidates = candidate_result_for_indexes(candidate_result, set(failed_indexes))
        repair_prompt_text = user_prompt(
            failed_briefs,
            style,
            public_lookup,
            entity_guide,
            repair_candidates,
            repair_context=repair_context,
        )
        repair_result = ask_deepseek(
            api_key,
            system_text,
            repair_prompt_text,
            temperature=0.15,
        )
        repaired_items = parse_items(repair_result)
        for index in failed_indexes:
            raw_item = repaired_items.get(index)
            if not raw_item:
                continue
            repaired = normalize_item(
                raw_item,
                clips[index - 1],
                entity_guide,
                research_item_for_index(entity_guide, index),
            )
            if not repaired:
                continue
            current_score = int(normalized.get(index, {}).get("title_quality_audit", {}).get("score", -1))
            repaired_score = int(repaired.get("title_quality_audit", {}).get("score", -1))
            if repaired.get("title_quality_audit", {}).get("pass") or repaired_score > current_score:
                normalized[index] = repaired
        repair_events.append({
            "indexes": failed_indexes,
            "user_prompt": repair_prompt_text,
            "raw_result": repair_result,
            "remaining_failed_indexes": [
                index
                for index in failed_indexes
                if index not in normalized or not normalized[index]["title_quality_audit"].get("pass")
            ],
        })

    if log_events is not None:
        log_events.append({
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "indexes": indexes,
            "briefs": briefs,
            "candidate_system_prompt": candidate_system_text,
            "candidate_user_prompt": candidate_prompt_text,
            "candidate_raw_result": candidate_result,
            "judge_system_prompt": system_text,
            "judge_user_prompt": prompt_text,
            "raw_result": result,
            "repair_events": repair_events,
            "parsed_indexes": sorted(parsed),
            "normalized": {
                str(index): {
                    "title": item.get("title"),
                    "title_lines": item.get("title_lines"),
                    "formula_id": item.get("formula_id"),
                    "angle_id": item.get("angle_id"),
                    "emotion_pole": item.get("emotion_pole"),
                    "viewer_reaction": item.get("viewer_reaction"),
                    "editor_scores": item.get("editor_scores"),
                    "quality_check": item.get("quality_check"),
                    "title_quality_audit": item.get("title_quality_audit"),
                    "comment": item.get("comment"),
                }
                for index, item in normalized.items()
            },
        })
    return normalized


def accept_near_miss_after_repair(refined: dict[str, Any]) -> bool:
    audit = refined.get("title_quality_audit")
    if not isinstance(audit, dict) or audit.get("pass"):
        return bool(isinstance(audit, dict) and audit.get("pass"))
    fixes = set(audit.get("fixes", []))
    thresholds = audit.get("semantic_thresholds")
    if not isinstance(thresholds, dict):
        thresholds = {}
    try:
        china_resonance = int(thresholds.get("china_resonance", 0))
    except (TypeError, ValueError):
        china_resonance = 0

    # DeepSeek's self-score can remain one point below target after all repair passes.
    # Accept only this narrow near-miss; deterministic hook, wording, source, and fact gates still hold.
    if fixes == {"make_china_direction_constructive_or_positive"} and china_resonance >= 7:
        audit["pass"] = True
        audit["pass_level"] = "accepted_after_repair"
        audit["strict_pass"] = False
        return True
    return False


def surgical_repair_user_prompt(
    brief: dict[str, Any],
    entity_guide: dict[str, Any],
    current: dict[str, Any],
    fixes: list[str],
) -> str:
    index = int(brief["index"])
    research = research_for_indexes(entity_guide, {index})
    return f"""Repair only the three-line Chinese cover title for clip {index}. Return strict JSON only.

Clip facts:
{json.dumps(compact_research_briefs([brief])[0], ensure_ascii=False, indent=2)}

Verified research:
{json.dumps(research, ensure_ascii=False, indent=2)}

Current rejected title:
{json.dumps({
    "title": current.get("title"),
    "title_lines": current.get("title_lines"),
    "angle_id": current.get("angle_id"),
    "emotion_pole": current.get("emotion_pole"),
    "viewer_reaction": current.get("viewer_reaction"),
    "evidence_basis": current.get("evidence_basis"),
    "editor_scores": current.get("editor_scores"),
}, ensure_ascii=False, indent=2)}

Code audit failures to fix:
{json.dumps(fixes, ensure_ascii=False)}

Return JSON:
{{
  "clips": [
    {{
      "index": {index},
      "angle_id": "surprise_reversal|outsider_candor|words_vs_actions|china_advantage|concrete_stakes|authority_breaks_consensus",
      "emotion_pole": "意外|惊喜|强疑惑|终于有人说|反差质疑|民族自豪",
      "viewer_reaction": "internal audience reaction only",
      "evidence_basis": ["specific supporting fact"],
      "title": "semantic equivalent of all three lines",
      "title_lines": ["specific subject", "unexpected fact or comparison", "concrete unresolved consequence"],
      "title_highlights": ["exact substring"],
      "runner_up_titles": ["one alternate"],
      "editor_scores": {{
        "emotion_tension": 8,
        "novelty": 7,
        "specificity": 7,
        "curiosity_gap": 7,
        "factual_fidelity": 9,
        "china_resonance": 8
      }},
      "quality_check": {{
        "has_recognizable_anchor": true,
        "has_specific_fact": true,
        "has_single_emotion_pole": true,
        "has_novelty_or_surprise": true,
        "has_curiosity_gap": true,
        "is_factually_supported": true,
        "passes_china_frame": true,
        "passes_wording_guard": true
      }}
    }}
  ]
}}

Surgical rules:
- Change only what is necessary to fix every listed audit failure. Preserve the strongest factual core.
- The final reader copy must directly show the subject, surprising fact/comparison, and unresolved consequence.
- angle_id, emotion_pole, viewer_reaction, outsider_candor, and the research process are internal metadata. Never spell the editorial reasoning out in title/title_lines.
- Never output 外资策略师、外资首席、海外专家、西方策略师、罕见直言、外媒点破、终于有人说、只有外资敢说、大实话、 or 西方机构承认.
- If the real person or institution is not a recognizable big name, omit that identity. Lead with the concrete subject.
- If make_the_emotional_reversal_visible_in_words is listed, at least one line must contain a specific factual reversal, forced choice, consequence question, or tension verb. A generic state such as 真实且加剧 is not enough.
- If remove_internal_editorial_labels_from_reader_copy is listed, replace the labels with the exact claim and its consequence; do not use synonyms for the same editorial narration.
- If remove_unverified_position_change_claim is listed, remove 改口 or any position-change synonym. Search-result absence never proves a prior position.
- If complete_the_tension_phrase_with_its_subject_or_object is listed, rewrite the line as a complete Chinese phrase; do not end on 逼急、逼到、迫使、 or 倒逼.
- For a supported China advantage, state the concrete advantage or the foreign competitor's response directly. Do not announce that the angle is patriotic.
- Use exactly three compact lines. Do not write comments or subtitle_comments; they will be preserved from the accepted draft.
- Do not use 回报路径在哪、回报存疑、前景如何、未来如何、值得关注、面临挑战、释放信号, or generic endings such as 有望扩大.
- Do not use financial-advice wording, hard crisis wording, source badges, sensitive geopolitical subjects, emojis, markdown, quotes, hashtags, or numbering.
- Every factual word must be supported by this clip or verified research. Use a question when causality is implied rather than explicit.
"""


def repair_refinement_surgically(
    api_key: str,
    brief: dict[str, Any],
    clip: dict[str, Any],
    entity_guide: dict[str, Any],
    current: dict[str, Any],
    *,
    log_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    fixes = list(current.get("title_quality_audit", {}).get("fixes", []))
    prompt_text = surgical_repair_user_prompt(brief, entity_guide, current, fixes)
    result = ask_deepseek(
        api_key,
        (
            "You are a precision Chinese cover-title repair editor. Return strict JSON only. "
            "Fix the listed audit failures without changing supported facts or exposing internal editorial labels. "
            + WORDING_GUARD_PROMPT
        ),
        prompt_text,
        temperature=0.1,
    )
    try:
        index = int(brief["index"])
    except (KeyError, TypeError, ValueError):
        return None
    raw = parse_items(result).get(index)
    if not raw:
        return None

    merged = dict(current)
    for key in (
        "formula_id",
        "angle_id",
        "emotion_pole",
        "viewer_reaction",
        "evidence_basis",
        "title",
        "title_lines",
        "title_highlights",
        "runner_up_titles",
        "editor_scores",
        "quality_check",
    ):
        if key in raw:
            merged[key] = raw[key]
    repaired = normalize_item(
        merged,
        clip,
        entity_guide,
        research_item_for_index(entity_guide, index),
    )
    if log_events is not None:
        log_events.append({
            "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "type": "surgical_title_repair",
            "indexes": [index],
            "briefs": [brief],
            "user_prompt": prompt_text,
            "raw_result": result,
            "normalized": repaired,
        })
    return repaired


def refine_titles(
    plan: dict[str, Any],
    *,
    api_key: str,
    style: str,
    batch_size: int,
    max_subtitles: int,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
    log_events: list[dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    clips = plan.get("clips", [])
    if not isinstance(clips, list) or not clips:
        raise SystemExit("No clips found in plan")

    refinements: dict[int, dict[str, Any]] = {}
    indexes = list(range(1, len(clips) + 1))
    for offset in range(0, len(indexes), batch_size):
        batch_indexes = indexes[offset:offset + batch_size]
        print(
            "Refining titles "
            + ",".join(str(index) for index in batch_indexes)
            + " with DeepSeek",
            flush=True,
        )
        refinements.update(
            refine_batch(
                api_key,
                plan,
                clips,
                batch_indexes,
                style=style,
                max_subtitles=max_subtitles,
                public_lookup=public_lookup,
                entity_guide=entity_guide,
                log_events=log_events,
            )
        )

    missing = [index for index in indexes if index not in refinements]
    for index in missing:
        print(f"Retrying missing title refinement for clip {index}", flush=True)
        refinements.update(
            refine_batch(
                api_key,
                plan,
                clips,
                [index],
                style=style,
                max_subtitles=max_subtitles,
                public_lookup=public_lookup,
                entity_guide=entity_guide,
                log_events=log_events,
            )
        )

    missing = [index for index in indexes if index not in refinements]
    if missing:
        raise SystemExit("DeepSeek did not return valid title refinements for clips: " + ",".join(map(str, missing)))

    quality_failed = [
        index
        for index in indexes
        if not refinements[index].get("title_quality_audit", {}).get("pass")
    ]
    for index in quality_failed:
        print(f"Surgically repairing title quality for clip {index}", flush=True)
        brief = clip_brief(plan, clips[index - 1], index, max_subtitles)
        retried = repair_refinement_surgically(
            api_key,
            brief,
            clips[index - 1],
            entity_guide=entity_guide,
            current=refinements[index],
            log_events=log_events,
        )
        if not retried:
            continue
        current_score = int(refinements[index].get("title_quality_audit", {}).get("score", -1))
        retried_score = int(retried.get("title_quality_audit", {}).get("score", -1))
        if retried.get("title_quality_audit", {}).get("pass") or retried_score > current_score:
            refinements[index] = retried

    quality_failed = [
        index
        for index in indexes
        if not refinements[index].get("title_quality_audit", {}).get("pass")
    ]
    for index in quality_failed:
        if accept_near_miss_after_repair(refinements[index]):
            print(
                f"Accepted clip {index} after repair with China resonance 7/10; all hard gates passed",
                flush=True,
            )

    quality_failed = [
        index
        for index in indexes
        if not refinements[index].get("title_quality_audit", {}).get("pass")
    ]
    if quality_failed:
        for index in quality_failed:
            rejected = refinements[index]
            print(
                "Rejected title after all repairs: "
                + json.dumps(
                    {
                        "index": index,
                        "title": rejected.get("title"),
                        "title_lines": rejected.get("title_lines"),
                        "angle_id": rejected.get("angle_id"),
                        "emotion_pole": rejected.get("emotion_pole"),
                        "viewer_reaction": rejected.get("viewer_reaction"),
                        "editor_scores": rejected.get("editor_scores"),
                        "audit": rejected.get("title_quality_audit"),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        details = "; ".join(
            f"{index}:{','.join(refinements[index].get('title_quality_audit', {}).get('fixes', []))}"
            for index in quality_failed
        )
        raise SystemExit("Title emotion-polarity quality gate failed: " + details)

    return refinements


def apply_refinements(
    plan: dict[str, Any],
    refinements: dict[int, dict[str, Any]],
    style: str,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
) -> None:
    clips = plan["clips"]
    for index, refined in refinements.items():
        clip = clips[index - 1]
        clip.setdefault("original_title", clip.get("title", ""))
        clip.setdefault("original_title_lines", clip.get("title_lines", []))
        clip.setdefault("original_title_highlights", clip.get("title_highlights", []))
        clip.setdefault("original_comment", clip.get("comment", ""))
        clip.setdefault("original_subtitle_comments", clip.get("subtitle_comments", []))
        clip["title"] = refined["title"]
        clip["title_lines"] = refined["title_lines"]
        clip["title_highlights"] = refined["title_highlights"]
        clip["comment"] = refined["comment"]
        clip["comment_highlights"] = refined["comment_highlights"]
        clip["subtitle_comments"] = refined["subtitle_comments"]
        clip["title_formula_id"] = refined.get("formula_id", "")
        clip["title_angle_id"] = refined.get("angle_id", "")
        clip["title_emotion_pole"] = refined.get("emotion_pole", "")
        clip["title_viewer_reaction"] = refined.get("viewer_reaction", "")
        clip["title_evidence_basis"] = refined.get("evidence_basis", [])
        clip["title_runner_up_titles"] = refined.get("runner_up_titles", [])
        clip["title_editor_scores"] = refined.get("editor_scores", {})
        clip["title_quality_audit"] = refined.get("title_quality_audit", {})
        clip["title_model_quality_check"] = refined.get("quality_check", {})
        clip["title_refined"] = True
        clip["title_refine_style"] = style

    sanitize_plan_wording(plan)

    plan["title_refine"] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "research_version": TITLE_RESEARCH_VERSION,
        "style": style,
        "clip_count": len(refinements),
        "entity_translation_guide": entity_guide,
        "public_lookup": public_lookup,
        "data_driven_lessons": TITLE_DATA_LESSONS,
        "quality_min_score": TITLE_QUALITY_MIN_SCORE,
        "quality_audit": [
            {
                "index": index,
                "title": clips[index - 1].get("title", ""),
                "angle_id": clips[index - 1].get("title_angle_id", ""),
                "emotion_pole": clips[index - 1].get("title_emotion_pole", ""),
                "viewer_reaction": clips[index - 1].get("title_viewer_reaction", ""),
                "editor_scores": clips[index - 1].get("title_editor_scores", {}),
                **(clips[index - 1].get("title_quality_audit") or {}),
            }
            for index in sorted(refinements)
        ],
    }


def default_log_path(plan_path: Path) -> Path:
    return plan_path.with_name("title_refine_log.json")


def write_refine_log(
    path: Path,
    *,
    plan_path: Path,
    style: str,
    public_lookup: list[dict[str, Any]],
    entity_guide: dict[str, Any],
    refinements: dict[int, dict[str, Any]],
    log_events: list[dict[str, Any]],
) -> None:
    payload = {
        "timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "plan_path": str(plan_path),
        "style": style,
        "provider": "deepseek",
        "model": "deepseek-chat",
        "research_version": TITLE_RESEARCH_VERSION,
        "data_driven_lessons": TITLE_DATA_LESSONS,
        "quality_min_score": TITLE_QUALITY_MIN_SCORE,
        "public_lookup_count": len(public_lookup),
        "entity_translation_guide": entity_guide,
        "refined_titles": {
            str(index): {
                "title": item.get("title"),
                "title_lines": item.get("title_lines"),
                "formula_id": item.get("formula_id"),
                "angle_id": item.get("angle_id"),
                "emotion_pole": item.get("emotion_pole"),
                "viewer_reaction": item.get("viewer_reaction"),
                "evidence_basis": item.get("evidence_basis"),
                "runner_up_titles": item.get("runner_up_titles"),
                "editor_scores": item.get("editor_scores"),
                "quality_check": item.get("quality_check"),
                "title_quality_audit": item.get("title_quality_audit"),
                "comment": item.get("comment"),
            }
            for index, item in sorted(refinements.items())
        },
        "events": log_events,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="highlight_plan.json to rewrite in place.")
    parser.add_argument("--style", default="factual clickbait for China finance audience")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-subtitles", type=int, default=DEFAULT_MAX_SUBTITLES)
    parser.add_argument("--lookup-max-queries", type=int, default=DEFAULT_LOOKUP_MAX_QUERIES)
    parser.add_argument("--lookup-results-per-query", type=int, default=DEFAULT_LOOKUP_RESULTS_PER_QUERY)
    parser.add_argument("--no-public-lookup", action="store_true")
    parser.add_argument("--log-path", type=Path, default=None, help="Write DeepSeek title-refine prompts/results to this JSON file.")
    args = parser.parse_args()

    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")
    if args.max_subtitles < 1:
        raise SystemExit("--max-subtitles must be at least 1")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    removed = remove_trump_clips_from_plan(plan, use_ai=True)
    if removed:
        print(f"Removed {len(removed)} sensitive-topic clip(s) before title refinement", flush=True)
        args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    clips = plan.get("clips", [])
    if not isinstance(clips, list) or not clips:
        raise SystemExit("No non-sensitive-topic clips found in plan")

    briefs = all_clip_briefs(plan, clips, args.max_subtitles)
    lookup: list[dict[str, Any]] = []
    research_queries: list[dict[str, Any]] = []
    if not args.no_public_lookup:
        print("Planning title research queries with DeepSeek", flush=True)
        research_queries = plan_public_research_queries(
            api_key,
            briefs,
            args.lookup_max_queries,
        )
        lookup = public_entity_lookup(
            research_queries,
            results_per_query=args.lookup_results_per_query,
        )
        if not lookup:
            print("No public entity lookup snippets collected; continuing with transcript context only.", flush=True)

    entity_guide = build_entity_translation_guide(api_key, briefs, lookup)
    entity_guide["research_queries"] = research_queries

    log_events: list[dict[str, Any]] = []
    refinements = refine_titles(
        plan,
        api_key=api_key,
        style=args.style,
        batch_size=args.batch_size,
        max_subtitles=args.max_subtitles,
        public_lookup=lookup,
        entity_guide=entity_guide,
        log_events=log_events,
    )
    apply_refinements(plan, refinements, args.style, lookup, entity_guide)
    removed = remove_trump_clips_from_plan(plan, use_ai=True)
    if removed:
        print(f"Removed {len(removed)} sensitive-topic clip(s) after title refinement", flush=True)
        if not plan.get("clips"):
            raise SystemExit("No non-sensitive-topic clips remained after title refinement")
    log_path = args.log_path or default_log_path(args.plan)
    plan["title_refine"]["log_file"] = str(log_path)
    args.plan.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_refine_log(
        log_path,
        plan_path=args.plan,
        style=args.style,
        public_lookup=lookup,
        entity_guide=entity_guide,
        refinements=refinements,
        log_events=log_events,
    )

    for index in sorted(refinements):
        refined = refinements[index]
        print(
            f"{index:02d}. {refined['title']} / {' | '.join(refined['title_lines'])} / {refined['comment']}",
            flush=True,
        )
    print(f"Wrote refined titles: {args.plan}", flush=True)
    print(f"Wrote title refine log: {log_path}", flush=True)


if __name__ == "__main__":
    main()

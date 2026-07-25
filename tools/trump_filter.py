#!/usr/bin/env python3
"""Shared filter for excluding sensitive geopolitical/military Bloomberg clips."""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Callable, Iterable

from deepseek_api import DeepSeekAPIError, request_deepseek_json

EXCLUDED_TOPIC_PATTERNS = [
    re.compile(r"\b(?:donald\s+(?:j\.?\s+)?)?trump(?:'s)?\b", re.IGNORECASE),
    re.compile(r"\bpresident\s+trump\b", re.IGNORECASE),
    re.compile(r"特朗普|川普|唐纳德[·\s-]?特朗普|唐納德[·\s-]?特朗普|川建国"),
    re.compile(
        r"\b(?:iran|iranian|tehran|strait\s+of\s+hormuz|hormuz|strait\s+of\s+hummus|"
        r"straighter\s+hummus|street\s+of\s+ramuz)\b",
        re.IGNORECASE,
    ),
    re.compile(r"伊朗|德黑兰|德黑蘭|霍尔木兹|霍爾木茲|霍穆兹|霍穆茲|荷姆兹|荷姆茲"),
    re.compile(
        r"\b(?:geopolitics?|geopolitical|military|missile|airstrike|drone\s+strike|warship|"
        r"navy|naval|army|air\s+force|armed\s+forces|pentagon|weapons?|munitions?|"
        r"patriot\s+(?:missile|interceptor)|ceasefire|armed\s+conflict|middle\s+east\s+conflict|"
        r"persian\s+gulf|red\s+sea|nuclear\s+(?:site|facility|program|talks?)|"
        r"gaza|hamas|hezbollah|houthi|ukraine|ukrainian|kyiv|kiev|crimea|"
        r"donbas|donetsk|luhansk|russia[-\s]?ukraine)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"地缘政治|地緣政治|军事|軍事|战争|戰爭|导弹|導彈|空袭|空襲|无人机|無人機|"
        r"核设施|核設施|停火|军舰|軍艦|海军|海軍|陆军|陸軍|空军|空軍|国防|國防|"
        r"武器|弹药|彈藥|以色列|加沙|哈马斯|哈瑪斯|真主党|真主黨|"
        r"胡塞|乌克兰|烏克蘭|基辅|基輔|克里米亚|克里米亞|顿巴斯|頓巴斯|俄乌|俄烏"
    ),
]
TRUMP_PATTERNS = EXCLUDED_TOPIC_PATTERNS
AI_CACHE: dict[str, dict[str, Any]] = {}


def iter_text(value: Any) -> Iterable[str]:
    if value is None:
        return
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_text(item)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            yield from iter_text(item)
        return
    if isinstance(value, (int, float, bool)):
        return
    yield str(value)


def excluded_topic_match(value: Any) -> str:
    text = "\n".join(iter_text(value))
    if not text:
        return ""
    for pattern in EXCLUDED_TOPIC_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


def trump_match(value: Any) -> str:
    return excluded_topic_match(value)


def compact_filter_text(value: Any, max_chars: int = 6000) -> str:
    text = "\n".join(iter_text(value))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        return text[:max_chars] + " ..."
    return text


def ask_deepseek_filter(text: str, api_key: str, timeout: int = 90) -> dict[str, Any]:
    cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if cache_key in AI_CACHE:
        return AI_CACHE[cache_key]

    system_prompt = (
        "You are a strict semantic content classifier for a Chinese finance short-video pipeline. "
        "Return strict JSON only."
    )
    user_prompt = f"""User rule:
Do not generate any video if the source video or clip involves sensitive geopolitics or military topics.

Classify whether this item should be excluded.

Exclude when:
- Donald Trump is a main or material topic, including his presidency, campaign, administration, legal cases, companies, social media, tariff agenda, immigration stance, Fed pressure, China policy, election chances, or market impact.
- The item discusses Iran, Iranian policy, Tehran, the Strait of Hormuz, Hormuz shipping, Middle East military escalation, missiles, drones, airstrikes, naval activity, ceasefires, nuclear facilities, armed conflict, war, or military operations.
- The item is materially about sensitive geopolitical confrontation, including Israel/Gaza/Hamas/Hezbollah/Houthi, Ukraine, the Russia-Ukraine war/fighting, sanctions tied to military conflict, or any active-conflict military story.
- The text uses indirect references that clearly point to these topics, such as former president, 47th president, MAGA, Mar-a-Lago, Truth Social, his tariffs, his White House, Persian Gulf chokepoint, oil route threat, red sea attacks, or nuclear site strikes, when context makes the reference clear.
- Chinese wording points to these topics, including 特朗普、川普、唐纳德特朗普、伊朗、霍尔木兹海峡、德黑兰、乌克兰、俄乌战争、俄乌冲突、地缘政治、军事、战争、导弹、空袭.

Do not exclude when:
- The item is ordinary macro, central-bank, earnings, trade, commodities, or market analysis without a clear connection to the sensitive topics above.
- A sensitive word appears only as a minor historical comparison and is not a material part of the clip.

Return JSON exactly:
{{
  "exclude": true,
  "confidence": 0.0,
  "reason": "short reason in Chinese"
}}

Text:
{text}
"""
    try:
        decision = request_deepseek_json(
            api_key,
            system_prompt,
            user_prompt,
            temperature=0,
            timeout=timeout,
            retry_delays=(2, 4),
            log_prefix="",
        )
        AI_CACHE[cache_key] = decision
        return decision
    except DeepSeekAPIError as exc:
        last_error = str(exc)
    decision = {"exclude": False, "confidence": 0.0, "reason": f"DeepSeek unavailable: {last_error}"}
    AI_CACHE[cache_key] = decision
    return decision


def semantic_excluded_topic_decision(value: Any, *, api_key: str | None = None) -> dict[str, Any]:
    text = compact_filter_text(value)
    if not text:
        return {"exclude": False, "confidence": 0.0, "reason": "empty text"}
    key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
    key = key.strip()
    if not key:
        return {"exclude": False, "confidence": 0.0, "reason": "DEEPSEEK_API_KEY not set"}
    decision = ask_deepseek_filter(text, key)
    try:
        confidence = float(decision.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return {
        "exclude": bool(decision.get("exclude")) and confidence >= 0.55,
        "confidence": confidence,
        "reason": str(decision.get("reason", "")),
    }


def semantic_trump_decision(value: Any, *, api_key: str | None = None) -> dict[str, Any]:
    return semantic_excluded_topic_decision(value, api_key=api_key)


def excluded_topic_decision(value: Any, *, use_ai: bool = False) -> dict[str, Any]:
    matched = excluded_topic_match(value)
    if matched:
        return {
            "exclude": True,
            "source": "keyword",
            "matched": matched,
            "confidence": 1.0,
            "reason": f"keyword matched: {matched}",
        }
    if use_ai:
        decision = semantic_excluded_topic_decision(value)
        if decision.get("exclude"):
            return {
                "exclude": True,
                "source": "deepseek",
                "matched": "semantic",
                "confidence": float(decision.get("confidence", 0.0)),
                "reason": str(decision.get("reason", "")),
            }
    return {"exclude": False, "source": "none", "matched": "", "confidence": 0.0, "reason": ""}


def trump_decision(value: Any, *, use_ai: bool = False) -> dict[str, Any]:
    return excluded_topic_decision(value, use_ai=use_ai)


def is_excluded_topic(*values: Any, use_ai: bool = False) -> bool:
    return excluded_topic_decision(values, use_ai=use_ai).get("exclude", False)


def is_trump_related(*values: Any, use_ai: bool = False) -> bool:
    return is_excluded_topic(*values, use_ai=use_ai)


def remove_excluded_topic_items(
    items: list[dict[str, Any]],
    *,
    text_getter: Callable[[dict[str, Any]], Any] | None = None,
    use_ai: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        haystack = text_getter(item) if text_getter else item
        decision = excluded_topic_decision(haystack, use_ai=use_ai)
        if decision.get("exclude"):
            removed.append({
                "index": index,
                "source": decision.get("source", ""),
                "matched": decision.get("matched", ""),
                "confidence": decision.get("confidence", 0.0),
                "reason": decision.get("reason", ""),
                "title": str(item.get("title", item.get("source_title", ""))),
                "url": str(item.get("url", item.get("source_url", ""))),
            })
            continue
        kept.append(item)
    return kept, removed


def remove_trump_items(
    items: list[dict[str, Any]],
    *,
    text_getter: Callable[[dict[str, Any]], Any] | None = None,
    use_ai: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    return remove_excluded_topic_items(items, text_getter=text_getter, use_ai=use_ai)


def remove_excluded_topic_clips_from_plan(plan: dict[str, Any], *, use_ai: bool = False) -> list[dict[str, Any]]:
    clips = plan.get("clips", [])
    if not isinstance(clips, list):
        return []
    kept, removed = remove_excluded_topic_items([clip for clip in clips if isinstance(clip, dict)], use_ai=use_ai)
    plan["clips"] = kept
    if removed:
        content_filter = plan.setdefault("content_filter", {})
        content_filter["sensitive_topics"] = {
            "enabled": True,
            "ai_enabled": use_ai,
            "removed_count": len(removed),
            "removed": removed,
        }
    return removed


def remove_trump_clips_from_plan(plan: dict[str, Any], *, use_ai: bool = False) -> list[dict[str, Any]]:
    return remove_excluded_topic_clips_from_plan(plan, use_ai=use_ai)

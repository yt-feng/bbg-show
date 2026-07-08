#!/usr/bin/env python3
"""Shared filter for excluding Trump-related Bloomberg clips."""

from __future__ import annotations

import re
import hashlib
import json
import os
import time
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
TRUMP_PATTERNS = [
    re.compile(r"\b(?:donald\s+(?:j\.?\s+)?)?trump(?:'s)?\b", re.IGNORECASE),
    re.compile(r"\bpresident\s+trump\b", re.IGNORECASE),
    re.compile(r"特朗普|川普|唐纳德[·\s-]?特朗普|唐納德[·\s-]?特朗普|川建国"),
]
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


def trump_match(value: Any) -> str:
    text = "\n".join(iter_text(value))
    if not text:
        return ""
    for pattern in TRUMP_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return ""


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
Do not generate any video if the source video or clip involves Donald Trump.

Classify whether this item should be excluded.

Exclude when:
- Donald Trump is a main or material topic.
- The item discusses his presidency, campaign, administration, family, companies, legal cases, social media, policy agenda, tariffs, immigration stance, Fed pressure, China policy, election chances, or market impact.
- The text uses indirect references that clearly point to him, such as former president, 47th president, MAGA, Mar-a-Lago, Truth Social, his tariffs, or his White House, when context makes the reference clear.
- Chinese wording points to him, including 特朗普、川普、唐纳德特朗普, or indirect phrasing clearly about him.

Do not exclude when:
- The item is only about the US, the White House, tariffs, Republicans, elections, or the Fed without a clear Trump connection.
- Trump appears only as a historical comparison and is not a material part of the clip.

Return JSON exactly:
{{
  "exclude": true,
  "confidence": 0.0,
  "reason": "short reason in Chinese"
}}

Text:
{text}
"""
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        DEEPSEEK_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    last_error = ""
    for attempt in range(3):
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read())
            content = raw["choices"][0]["message"]["content"]
            decision = json.loads(content)
            if not isinstance(decision, dict):
                decision = {}
            AI_CACHE[cache_key] = decision
            return decision
        except (HTTPError, URLError, TimeoutError, OSError, KeyError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    decision = {"exclude": False, "confidence": 0.0, "reason": f"DeepSeek unavailable: {last_error}"}
    AI_CACHE[cache_key] = decision
    return decision


def semantic_trump_decision(value: Any, *, api_key: str | None = None) -> dict[str, Any]:
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


def trump_decision(value: Any, *, use_ai: bool = False) -> dict[str, Any]:
    matched = trump_match(value)
    if matched:
        return {
            "exclude": True,
            "source": "keyword",
            "matched": matched,
            "confidence": 1.0,
            "reason": f"keyword matched: {matched}",
        }
    if use_ai:
        decision = semantic_trump_decision(value)
        if decision.get("exclude"):
            return {
                "exclude": True,
                "source": "deepseek",
                "matched": "semantic",
                "confidence": float(decision.get("confidence", 0.0)),
                "reason": str(decision.get("reason", "")),
            }
    return {"exclude": False, "source": "none", "matched": "", "confidence": 0.0, "reason": ""}


def is_trump_related(*values: Any, use_ai: bool = False) -> bool:
    return trump_decision(values, use_ai=use_ai).get("exclude", False)


def remove_trump_items(
    items: list[dict[str, Any]],
    *,
    text_getter: Callable[[dict[str, Any]], Any] | None = None,
    use_ai: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        haystack = text_getter(item) if text_getter else item
        decision = trump_decision(haystack, use_ai=use_ai)
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


def remove_trump_clips_from_plan(plan: dict[str, Any], *, use_ai: bool = False) -> list[dict[str, Any]]:
    clips = plan.get("clips", [])
    if not isinstance(clips, list):
        return []
    kept, removed = remove_trump_items([clip for clip in clips if isinstance(clip, dict)], use_ai=use_ai)
    plan["clips"] = kept
    if removed:
        content_filter = plan.setdefault("content_filter", {})
        content_filter["trump"] = {
            "enabled": True,
            "ai_enabled": use_ai,
            "removed_count": len(removed),
            "removed": removed,
        }
    return removed

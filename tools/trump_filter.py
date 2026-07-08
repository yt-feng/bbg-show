#!/usr/bin/env python3
"""Shared filter for excluding Trump-related Bloomberg clips."""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable


TRUMP_PATTERNS = [
    re.compile(r"\b(?:donald\s+(?:j\.?\s+)?)?trump(?:'s)?\b", re.IGNORECASE),
    re.compile(r"\bpresident\s+trump\b", re.IGNORECASE),
    re.compile(r"特朗普|川普|唐纳德[·\s-]?特朗普|唐納德[·\s-]?特朗普|川建国"),
]


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


def is_trump_related(*values: Any) -> bool:
    return any(trump_match(value) for value in values)


def remove_trump_items(
    items: list[dict[str, Any]],
    *,
    text_getter: Callable[[dict[str, Any]], Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    kept: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []
    for index, item in enumerate(items, start=1):
        haystack = text_getter(item) if text_getter else item
        matched = trump_match(haystack)
        if matched:
            removed.append({
                "index": index,
                "matched": matched,
                "title": str(item.get("title", item.get("source_title", ""))),
                "url": str(item.get("url", item.get("source_url", ""))),
            })
            continue
        kept.append(item)
    return kept, removed


def remove_trump_clips_from_plan(plan: dict[str, Any]) -> list[dict[str, Any]]:
    clips = plan.get("clips", [])
    if not isinstance(clips, list):
        return []
    kept, removed = remove_trump_items([clip for clip in clips if isinstance(clip, dict)])
    plan["clips"] = kept
    if removed:
        content_filter = plan.setdefault("content_filter", {})
        content_filter["trump"] = {
            "enabled": True,
            "removed_count": len(removed),
            "removed": removed,
        }
    return removed

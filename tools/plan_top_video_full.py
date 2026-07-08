#!/usr/bin/env python3
"""Build a one-clip bilingual plan for a finance/news source video."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from plan_speaker_full import (  # noqa: E402
    clean_text,
    load_transcript_units,
    normalize_highlights,
    safe_zh,
    translate_units,
)
from plan_speaker_highlights import ask_deepseek  # noqa: E402
from trump_filter import is_trump_related, remove_trump_clips_from_plan  # noqa: E402
from wording_guard import WORDING_GUARD_PROMPT, sanitize_plan_wording  # noqa: E402


TITLE_BADGE_PATTERNS = [
    re.compile(r"[【\[]\s*(?:彭博社?|Bloomberg)?\s*独家\s*[】\]]\s*", re.IGNORECASE),
    re.compile(r"^(?:彭博社?|Bloomberg)?\s*独家\s*[：:｜|\-、\s]*", re.IGNORECASE),
    re.compile(r"\bBloomberg\s+Exclusive\b\s*[：:｜|\-、\s]*", re.IGNORECASE),
    re.compile(r"彭博社?独家\s*[：:｜|\-、\s]*"),
]


def strip_title_badges(text: str) -> str:
    value = clean_text(text)
    for pattern in TITLE_BADGE_PATTERNS:
        value = pattern.sub("", value)
    value = value.replace("【】", "").replace("[]", "")
    return clean_text(value.strip(" ：:｜|-、"))


def transcript_sample(transcript: Path, limit: int = 3000) -> str:
    data = json.loads(transcript.read_text(encoding="utf-8"))
    lines: list[str] = []
    for segment in data.get("segments", []):
        text = clean_text(str(segment.get("text", "")))
        if not text:
            continue
        lines.append(f"{float(segment['start']):.1f}-{float(segment['end']):.1f}: {text}")
        if len("\n".join(lines)) >= limit:
            break
    return "\n".join(lines)[:limit]


def choose_clip_range(
    transcript: Path,
    segment_start: float,
    segment_end: float,
    max_clip_seconds: float,
) -> tuple[float, float]:
    if max_clip_seconds <= 0 or segment_end - segment_start <= max_clip_seconds:
        return segment_start, segment_end

    target_end = segment_start + max_clip_seconds
    data = json.loads(transcript.read_text(encoding="utf-8"))
    segments = [
        seg for seg in data.get("segments", [])
        if float(seg.get("end", 0)) > segment_start and float(seg.get("start", 0)) < target_end
    ]
    if not segments:
        return segment_start, min(segment_end, target_end)

    latest_end = segment_start
    sentence_end = segment_start
    preferred_floor = max(segment_start + 60.0, target_end - 12.0)
    for seg in segments:
        raw_end = float(seg.get("end", 0))
        end = min(target_end, raw_end)
        if end <= segment_start:
            continue
        latest_end = max(latest_end, end)
        text = clean_text(str(seg.get("text", "")))
        if (
            raw_end <= target_end
            and end >= preferred_floor
            and text.endswith((".", "?", "!", "。", "？", "！"))
        ):
            sentence_end = max(sentence_end, end)

    clip_end = sentence_end if sentence_end > segment_start else latest_end
    if clip_end <= segment_start:
        clip_end = min(segment_end, target_end)
    return segment_start, min(segment_end, target_end, clip_end)


def fallback_title_lines(source_title: str, source_label: str = "Bloomberg Top") -> list[str]:
    label = clean_text(source_label) or "Bloomberg Top"
    title = clean_text(source_title) or f"{label} Video"
    words = title.split()
    if len(words) >= 6:
        midpoint = max(2, len(words) // 2)
        return [
            label[:24],
            " ".join(words[:midpoint])[:22],
            " ".join(words[midpoint:])[:22],
        ]
    return [label[:24], title[:22], "今日热点"]


def generate_title(source_title: str, source_url: str, source_label: str, sample: str) -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    system_prompt = (
        "You are a Chinese finance short-video title editor. Return strict JSON only. "
        "Write concise Simplified Chinese titles suitable for a vertical finance/news clip. "
        "Avoid sensitive Chinese words: rephrase 投资/股票/A股/港股/美股 when needed. "
        "Never use source badges such as 彭博独家, 独家, 【彭博独家】, or Bloomberg Exclusive. "
        + WORDING_GUARD_PROMPT
    )
    user_prompt = f"""Source label:
{source_label}

Source title:
{source_title}

Source URL:
{source_url}

Transcript sample:
{sample}

Return JSON:
{{
  "title": "完整中文标题",
  "title_lines": ["第一行", "第二行", "第三行"],
  "title_highlights": ["关键词1", "关键词2"]
}}

Rules:
- title_lines must contain exactly 3 short display lines.
- Line 1 should name the main actor, institution, or event. Do not use source labels.
- Line 2 should name the event or asset/topic.
- Line 3 should be a hook or concrete angle.
- title_highlights must be exact substrings from the joined title_lines.
- Never write 彭博独家, 独家, 【彭博独家】, or Bloomberg Exclusive.
- Never use hard crisis/doom wording such as 经济危机、金融危机、债务危机、危机、崩盘、崩溃 in Chinese titles.
- Prefer softer market wording such as 流动性变化、政策信号、需求变化、信心修复、估值重估、周期压力.
- For China-related topics, keep pressure factual but word it as market/policy/liquidity changes, not China decline.
- Do not produce any title/comment/subtitle for Donald Trump / Trump / 特朗普 / 川普 content.
- Do not add markdown.
"""
    result = ask_deepseek(api_key, system_prompt, user_prompt, temperature=0.2)
    lines = result.get("title_lines")
    title = clean_text(str(result.get("title", "")))
    if not isinstance(lines, list) or len(lines) != 3:
        lines = fallback_title_lines(source_title, source_label)
    fallback_lines = fallback_title_lines(source_title, source_label)
    lines = [
        safe_zh(strip_title_badges(str(line)))[:24] or fallback_lines[index]
        for index, line in enumerate(lines)
    ]
    if not title:
        title = "：".join(line for line in lines if line)
    title = safe_zh(strip_title_badges(title))
    if not title:
        title = "：".join(line for line in lines if line)
    joined = "".join(lines)
    highlights = normalize_highlights(result.get("title_highlights"), joined, limit=3)
    if not highlights:
        highlights = [line for line in lines[1:3] if line][:2]
    return {
        "title": title,
        "title_lines": lines,
        "title_highlights": [safe_zh(item) for item in highlights],
    }


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    if is_trump_related(args.source_title, args.source_url, use_ai=True):
        raise SystemExit("Source video skipped by Trump filter")

    clip_start, clip_end = choose_clip_range(
        args.transcript,
        args.segment_start,
        args.segment_end,
        args.max_clip_seconds,
    )
    if clip_end < args.segment_end:
        print(
            f"Clipping top video plan to {clip_end - clip_start:.1f}s "
            f"from source segment {args.segment_end - args.segment_start:.1f}s",
            flush=True,
        )

    units = load_transcript_units(
        args.transcript,
        clip_start,
        clip_end,
        min_seconds=args.subtitle_min_seconds,
        max_seconds=args.subtitle_max_seconds,
        max_chars=args.subtitle_max_chars,
    )
    print(f"Built {len(units)} subtitle units", flush=True)
    title_info = generate_title(args.source_title, args.source_url, args.source_label, transcript_sample(args.transcript))
    translations = translate_units(
        units,
        args.speaker,
        args.source_title,
        args.batch_size,
    )

    subtitles: list[dict[str, Any]] = []
    for unit in units:
        item = translations.get(unit.index, {})
        zh = safe_zh(str(item.get("zh", "")))
        if not zh:
            raise SystemExit(f"Missing Chinese translation for subtitle {unit.index}")
        en = clean_text(unit.en)
        subtitles.append({
            "index": unit.index,
            "start": unit.start,
            "end": unit.end,
            "relative_start": unit.start - clip_start,
            "relative_end": unit.end - clip_start,
            "en": en,
            "zh": zh,
            "zh_highlights": normalize_highlights(item.get("zh_highlights"), zh),
            "en_highlights": normalize_highlights(item.get("en_highlights"), en),
        })

    plan = {
        "source_url": args.source_url,
        "source_title": args.source_title,
        "speaker": args.speaker,
        "speaker_context": args.source_title,
        "source_transcript": str(args.transcript),
        "source_segment_range": [args.segment_start, args.segment_end],
        "segment_range": [clip_start, clip_end],
        "duration": clip_end - clip_start,
        "clips": [
            {
                "start": clip_start,
                "end": clip_end,
                "speaker": args.speaker,
                "title": title_info["title"],
                "title_lines": title_info["title_lines"],
                "title_highlights": title_info["title_highlights"],
                "subtitles": subtitles,
            }
        ],
    }
    plan = sanitize_plan_wording(plan)
    removed = remove_trump_clips_from_plan(plan, use_ai=True)
    if removed:
        print(f"Removed {len(removed)} Trump-related top-video clip(s)", flush=True)
    if not plan.get("clips"):
        raise SystemExit("No non-Trump clips remained after filtering")
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-title", required=True)
    parser.add_argument("--source-label", default="Bloomberg Top")
    parser.add_argument("--speaker", default="Bloomberg")
    parser.add_argument("--segment-start", type=float, default=0.0)
    parser.add_argument("--segment-end", type=float, required=True)
    parser.add_argument("--max-clip-seconds", type=float, default=90.0)
    parser.add_argument("--subtitle-min-seconds", type=float, default=3.0)
    parser.add_argument("--subtitle-max-seconds", type=float, default=7.5)
    parser.add_argument("--subtitle-max-chars", type=int, default=220)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.segment_end <= args.segment_start:
        raise SystemExit("--segment-end must be greater than --segment-start")

    plan = build_plan(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote top video plan: {args.out}", flush=True)


if __name__ == "__main__":
    main()

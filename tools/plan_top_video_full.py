#!/usr/bin/env python3
"""Build a one-clip bilingual plan for a Bloomberg Top Video."""

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


def fallback_title_lines(source_title: str) -> list[str]:
    title = clean_text(source_title) or "Bloomberg Top Video"
    words = title.split()
    if len(words) >= 6:
        midpoint = max(2, len(words) // 2)
        return [
            "Bloomberg Top",
            " ".join(words[:midpoint])[:22],
            " ".join(words[midpoint:])[:22],
        ]
    return ["Bloomberg Top", title[:22], "今日热点"]


def generate_title(source_title: str, source_url: str, sample: str) -> dict[str, Any]:
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    system_prompt = (
        "You are a Chinese finance short-video title editor. Return strict JSON only. "
        "Write concise Simplified Chinese titles suitable for a vertical Bloomberg news clip. "
        "Avoid sensitive Chinese words: rephrase 投资/股票/A股/港股/美股 when needed."
    )
    user_prompt = f"""Bloomberg source title:
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
- Line 1 should identify Bloomberg/人物/机构 if useful.
- Line 2 should name the event or asset/topic.
- Line 3 should be a hook or concrete angle.
- title_highlights must be exact substrings from the joined title_lines.
- Do not add markdown.
"""
    result = ask_deepseek(api_key, system_prompt, user_prompt, temperature=0.2)
    lines = result.get("title_lines")
    title = clean_text(str(result.get("title", "")))
    if not isinstance(lines, list) or len(lines) != 3:
        lines = fallback_title_lines(source_title)
    lines = [safe_zh(clean_text(str(line)))[:24] for line in lines]
    if not title:
        title = "：".join(line for line in lines if line)
    title = safe_zh(title)
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
    units = load_transcript_units(
        args.transcript,
        args.segment_start,
        args.segment_end,
        min_seconds=args.subtitle_min_seconds,
        max_seconds=args.subtitle_max_seconds,
        max_chars=args.subtitle_max_chars,
    )
    print(f"Built {len(units)} subtitle units", flush=True)
    title_info = generate_title(args.source_title, args.source_url, transcript_sample(args.transcript))
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
            "relative_start": unit.start - args.segment_start,
            "relative_end": unit.end - args.segment_start,
            "en": en,
            "zh": zh,
            "zh_highlights": normalize_highlights(item.get("zh_highlights"), zh),
            "en_highlights": normalize_highlights(item.get("en_highlights"), en),
        })

    return {
        "source_url": args.source_url,
        "source_title": args.source_title,
        "speaker": args.speaker,
        "speaker_context": args.source_title,
        "source_transcript": str(args.transcript),
        "segment_range": [args.segment_start, args.segment_end],
        "duration": args.segment_end - args.segment_start,
        "clips": [
            {
                "start": args.segment_start,
                "end": args.segment_end,
                "speaker": args.speaker,
                "title": title_info["title"],
                "title_lines": title_info["title_lines"],
                "title_highlights": title_info["title_highlights"],
                "subtitles": subtitles,
            }
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-title", required=True)
    parser.add_argument("--speaker", default="Bloomberg")
    parser.add_argument("--segment-start", type=float, default=0.0)
    parser.add_argument("--segment-end", type=float, required=True)
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

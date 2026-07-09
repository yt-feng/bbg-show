#!/usr/bin/env python3
"""Generate a highlight_plan.json for a speaker segment using DeepSeek.

Outputs the exact format expected by render_clips_linux.py:
- clips[].start, end, title, title_lines, title_highlights
- clips[].subtitles[].start, end, relative_start, relative_end, zh, en, zh_highlights, en_highlights
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from trump_filter import remove_trump_clips_from_plan
from wording_guard import WORDING_GUARD_PROMPT, sanitize_plan_wording


DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
HOST_OUTRO_PATTERNS = [
    r"\bthank you\b",
    r"\bleave it there\b",
    r"\bhere'?s a look\b",
    r"\basian markets\b",
    r"\bmarkets are doing\b",
    r"\bas we head into\b",
    r"到此为止",
    r"非常感谢",
    r"亚洲市场",
    r"开盘情况",
]
SHORT_CLIP_EXPAND_MIN_RATIO = 0.75
SHORT_CLIP_EXPAND_MAX_DEFICIT = 10.0


def ask_deepseek(api_key: str, system_prompt: str, user_prompt: str, temperature: float = 0.3) -> dict:
    payload = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    body = json.dumps(payload).encode()
    req = Request(
        DEEPSEEK_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    for attempt in range(3):
        try:
            with urlopen(req, timeout=180) as resp:
                result = json.loads(resp.read())
            content = result["choices"][0]["message"]["content"]
            return json.loads(content)
        except (HTTPError, URLError, json.JSONDecodeError, KeyError) as exc:
            print(f"  DeepSeek attempt {attempt + 1} failed: {exc}", flush=True)
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
            else:
                raise SystemExit(f"DeepSeek API failed: {exc}")
    return {}


def format_time(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}:{s:05.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True, help="Transcript JSON file")
    parser.add_argument("--speaker", type=str, required=True)
    parser.add_argument("--speaker-context", type=str, default="")
    parser.add_argument("--segment-start", type=float, required=True)
    parser.add_argument("--segment-end", type=float, required=True)
    parser.add_argument("--min-seconds", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=90)
    parser.add_argument("--min-clips", type=int, default=0, help="Preferred minimum number of clips; 0 disables.")
    parser.add_argument("--max-clips", type=int, default=0, help="Maximum number of clips to keep; 0 disables.")
    parser.add_argument("--out", type=Path, required=True, help="Output highlight_plan.json path")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.out.exists() and not args.force:
        print(f"Using cached plan: {args.out}", flush=True)
        return

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY is required")

    # Load transcript and filter to segment range
    data = json.loads(args.transcript.read_text(encoding="utf-8"))
    all_segments = data.get("segments", [])
    segments = [
        s for s in all_segments
        if float(s["end"]) >= args.segment_start and float(s["start"]) <= args.segment_end
    ]
    if not segments:
        raise SystemExit("No transcript segments in the given range")

    transcript_lines = []
    for s in segments:
        transcript_lines.append(f"{s['start']:.1f}-{s['end']:.1f}: {s['text'].strip()}")
    transcript_text = "\n".join(transcript_lines)

    segment_duration = args.segment_end - args.segment_start
    print(f"Planning for {args.speaker}: {args.segment_start:.0f}s-{args.segment_end:.0f}s ({segment_duration:.0f}s)", flush=True)
    print(f"Transcript segments in range: {len(segments)}", flush=True)

    # Step 1: Ask DeepSeek to split into clips with bilingual subtitles
    system_prompt = (
        "You are a senior Chinese short-video editor. Your task is to split an interview segment "
        "into short clips and provide bilingual subtitles with keyword highlights. "
        "Return strict JSON only. All Chinese title fields must avoid financial-advice/product-sale words: "
        "rephrase 资产管理/投资/股票/基金/理财/保险 as neutral market wording. "
        "For subtitles, rephrase 投资/股票/A股/港股 with 配置/权益资产/内地市场/香港市场 when needed. "
        + WORDING_GUARD_PROMPT
    )

    user_prompt = f"""Speaker: {args.speaker}
Context: {args.speaker_context}
Segment: {args.segment_start:.1f}s - {args.segment_end:.1f}s (total {segment_duration:.0f}s)

Transcript:
{transcript_text}

---

Task:
1. Select the strongest short-video highlight clips of {args.min_seconds}-{args.max_seconds} seconds each. Each clip should start/end at natural topic boundaries.
2. For each clip, provide:
   - A Chinese title (机构/嘉宾身份 + 热点事件 + hook结构, 例如 "高盛王逸：5万亿城市更新，会拖住楼市吗？")
   - title_lines: split the title into exactly 3 short lines for display (line1=机构嘉宾, line2=主题关键词, line3=hook/观点)
   - title_highlights: 2-3 keywords from title_lines to highlight in yellow
   - Bilingual subtitles covering the full clip duration

3. For each subtitle entry:
   - Provide the original English text
   - Provide a natural Chinese translation (avoid 投资/股 words when possible)
   - zh_highlights: 1-2 key Chinese phrases to highlight yellow
   - en_highlights: 1-2 key English phrases to highlight yellow
   - Each subtitle should be 3-8 seconds long

Return JSON:
{{
  "clips": [
    {{
      "start": <absolute seconds>,
      "end": <absolute seconds>,
      "speaker": "{args.speaker}",
      "title": "<full Chinese title with colon>",
      "title_lines": ["<line1>", "<line2>", "<line3>"],
      "title_highlights": ["<keyword1>", "<keyword2>"],
      "subtitles": [
        {{
          "index": 1,
          "start": <absolute seconds>,
          "end": <absolute seconds>,
          "en": "<English text>",
          "zh": "<Chinese translation>",
          "zh_highlights": ["<phrase>"],
          "en_highlights": ["<phrase>"]
        }}
      ]
    }}
  ]
}}

Important:
- Timestamps are ABSOLUTE (from video start, not segment start)
- Subtitles must cover the full clip without gaps
- Return {clip_count_rule(args.min_clips, args.max_clips)}. If the interview genuinely cannot support the minimum without filler, return fewer high-quality clips.
- Each clip must have at least 3 subtitles
- Every clip must contain a substantive answer from {args.speaker}. A host question is allowed only if the speaker answer follows in the same clip.
- Do not create clips from host outros, thank-you lines, post-interview market recaps, market open boards, or transitions to the next segment. If the provided range includes that material, stop before it and return fewer clips.
- Do not create clips about sensitive geopolitics or military topics, including Donald Trump / Trump / 特朗普 / 川普, Iran / 伊朗, Strait of Hormuz / 霍尔木兹海峡, wars, missiles, airstrikes, military operations, or active-conflict stories. If the strongest segment is sensitive-topic related, return fewer clips or no clips.
- Do not reuse the same subtitle text in multiple clips unless it genuinely appears twice in the source transcript.
- Chinese titles must have hook/conflict angle, not flat descriptions
- Avoid in Chinese title fields: 资产管理, 投资, 股票, 基金, 理财, 保险, 投顾, 荐股, 买入, 卖出.
- Rephrase title wording with neutral alternatives such as 资管, 配置, 权益资产, 市场, 产品, 财富配置, 保障, 观点.
- Avoid: 投资, 股票, A股, 港股, 美股 in Chinese subtitles when possible.
- Never use hard crisis/doom wording in Chinese title/subtitle/comment fields: 经济危机、金融危机、债务危机、危机、崩盘、崩溃、完了、没救、惨了.
- Prefer softer wording: 流动性变化、信贷变化、政策信号、需求变化、信心修复、估值重估、周期压力、结构调整、市场波动.
- If the segment is China-related and negative, word the Chinese subtitles around pressure, policy response, demand repair, liquidity change, or confidence repair. Do not make China itself sound hopeless or ridiculed.
"""

    print("Requesting clip plan from DeepSeek...", flush=True)
    result = ask_deepseek(api_key, system_prompt, user_prompt)
    clips = result.get("clips", [])

    if not clips:
        print("DeepSeek returned no clips; building transcript fallback clip", flush=True)

    clips = postprocess_clips(
        clips,
        args.segment_start,
        args.segment_end,
        args.min_seconds,
        args.max_seconds,
        args.max_clips,
    )
    filter_probe = {"clips": clips}
    removed = remove_trump_clips_from_plan(filter_probe, use_ai=True)
    if removed:
        print(f"Removed {len(removed)} sensitive-topic planned clip(s)", flush=True)
    clips = filter_probe["clips"]
    if not clips:
        print("All generated clips failed the speaker-content quality gate", flush=True)
        fallback_clip = build_transcript_fallback_clip(
            segments,
            args.speaker,
            args.speaker_context,
            args.segment_start,
            args.segment_end,
            args.min_seconds,
            args.max_seconds,
        )
        if fallback_clip:
            filter_probe = {"clips": [fallback_clip]}
            removed = remove_trump_clips_from_plan(filter_probe, use_ai=True)
            if filter_probe["clips"]:
                clips = filter_probe["clips"]
                print("Using transcript fallback clip", flush=True)
            else:
                raise SystemExit("All generated clips were sensitive-topic related")
        else:
            raise SystemExit("All generated clips failed the speaker-content quality gate")

    # Write output
    payload = sanitize_plan_wording({
        "speaker": args.speaker,
        "speaker_context": args.speaker_context,
        "source_transcript": str(args.transcript),
        "segment_range": [args.segment_start, args.segment_end],
        "duration": segment_duration,
        "clips": clips,
    })
    removed = remove_trump_clips_from_plan(payload, use_ai=True)
    if removed:
        print(f"Removed {len(removed)} sensitive-topic clip(s) after wording guard", flush=True)
    if not payload.get("clips"):
        raise SystemExit("No non-sensitive-topic clips remained after filtering")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote plan: {args.out} ({len(clips)} clips)", flush=True)


def clip_count_rule(min_clips: int, max_clips: int) -> str:
    if min_clips > 0 and max_clips > 0:
        return f"{min_clips}-{max_clips} clips total"
    if max_clips > 0:
        return f"up to {max_clips} clips total"
    if min_clips > 0:
        return f"at least {min_clips} clips total"
    return "as many clips as the segment naturally supports"


def postprocess_clips(
    clips: list[dict[str, Any]],
    segment_start: float,
    segment_end: float,
    min_seconds: int,
    max_seconds: int,
    max_clips: int,
) -> list[dict[str, Any]]:
    cleaned: list[dict[str, Any]] = []
    for idx, clip in enumerate(clips, start=1):
        try:
            start = max(segment_start, float(clip["start"]))
            end = min(segment_end, float(clip["end"]))
        except (KeyError, TypeError, ValueError):
            print(f"Skipping clip {idx}: invalid start/end", flush=True)
            continue

        if end <= start:
            print(f"Skipping clip {idx}: empty after segment clamp", flush=True)
            continue

        duration = end - start
        if min_seconds > 0 and duration < min_seconds:
            expanded = expand_short_clip(start, end, segment_start, segment_end, min_seconds, max_seconds)
            if expanded is None:
                print(f"Skipping clip {idx}: too short ({duration:.1f}s < {min_seconds}s)", flush=True)
                continue
            start, end = expanded
            expanded_duration = end - start
            print(
                f"Extending clip {idx}: {duration:.1f}s -> {expanded_duration:.1f}s",
                flush=True,
            )
            duration = expanded_duration
        if max_seconds > 0 and duration > max_seconds:
            print(f"Skipping clip {idx}: too long ({duration:.1f}s > {max_seconds}s)", flush=True)
            continue

        if is_host_outro_or_market_recap(clip):
            print(f"Skipping clip {idx}: host outro or market recap detected", flush=True)
            continue

        subtitles = []
        for sub_idx, sub in enumerate(clip.get("subtitles", []), start=1):
            try:
                sub_start = max(start, float(sub["start"]))
                sub_end = min(end, float(sub["end"]))
            except (KeyError, TypeError, ValueError):
                print(f"  Skipping subtitle {sub_idx} in clip {idx}: invalid start/end", flush=True)
                continue
            if sub_end <= sub_start:
                continue
            normalized = dict(sub)
            normalized["start"] = sub_start
            normalized["end"] = sub_end
            normalized["relative_start"] = sub_start - start
            normalized["relative_end"] = sub_end - start
            subtitles.append(normalized)

        if len(subtitles) < 2:
            print(f"Skipping clip {idx}: fewer than 2 valid subtitles", flush=True)
            continue

        normalized_clip = dict(clip)
        normalized_clip["start"] = start
        normalized_clip["end"] = end
        normalized_clip["subtitles"] = subtitles
        cleaned.append(normalized_clip)

    if max_clips > 0:
        cleaned = cleaned[:max_clips]
    return cleaned


def expand_short_clip(
    start: float,
    end: float,
    segment_start: float,
    segment_end: float,
    min_seconds: int,
    max_seconds: int,
) -> Optional[Tuple[float, float]]:
    """Expand near-miss DeepSeek clips to the configured minimum duration."""
    duration = end - start
    if min_seconds <= 0 or duration >= min_seconds:
        return start, end

    deficit = min_seconds - duration
    if duration < min_seconds * SHORT_CLIP_EXPAND_MIN_RATIO or deficit > SHORT_CLIP_EXPAND_MAX_DEFICIT:
        return None
    if segment_end - segment_start < min_seconds:
        return None
    if max_seconds > 0 and min_seconds > max_seconds:
        return None

    target = float(min_seconds)
    new_start = max(segment_start, start - deficit / 2)
    new_end = min(segment_end, end + deficit / 2)

    if new_end - new_start < target and new_start <= segment_start:
        new_end = min(segment_end, new_start + target)
    if new_end - new_start < target and new_end >= segment_end:
        new_start = max(segment_start, new_end - target)

    if new_end - new_start < target:
        return None
    return new_start, new_end


def build_transcript_fallback_clip(
    segments: list[dict[str, Any]],
    speaker: str,
    speaker_context: str,
    segment_start: float,
    segment_end: float,
    min_seconds: int,
    max_seconds: int,
) -> Optional[dict[str, Any]]:
    useful_segments = fallback_segments(segments, segment_start, segment_end, min_seconds)
    if not useful_segments:
        return None

    available_start = useful_segments[0]["start"]
    available_end = useful_segments[-1]["end"]
    available_duration = available_end - available_start
    if min_seconds > 0 and available_duration < min_seconds:
        return None

    clip_start = available_start
    clip_end = available_end
    if max_seconds > 0 and clip_end - clip_start > max_seconds:
        clip_end = clip_start + max_seconds

    subtitles: list[dict[str, Any]] = []
    for segment in useful_segments:
        sub_start = max(clip_start, float(segment["start"]))
        sub_end = min(clip_end, float(segment["end"]))
        if sub_end <= sub_start:
            continue
        text = clean_segment_text(str(segment.get("text", "")))
        if not text:
            continue
        subtitles.append({
            "index": len(subtitles) + 1,
            "start": sub_start,
            "end": sub_end,
            "relative_start": sub_start - clip_start,
            "relative_end": sub_end - clip_start,
            "en": text,
            "zh": text,
            "zh_highlights": [],
            "en_highlights": [],
        })

    if not subtitles:
        return None

    context_line = compact_title_line(speaker_context or "Bloomberg interview", 18)
    title_lines = [
        compact_title_line(speaker, 16),
        context_line,
        "核心观点速览",
    ]
    return {
        "start": clip_start,
        "end": clip_end,
        "speaker": speaker,
        "title": f"{speaker}: 核心观点速览",
        "title_lines": title_lines,
        "title_highlights": ["核心观点"],
        "subtitles": subtitles,
        "fallback": True,
    }


def fallback_segments(
    segments: list[dict[str, Any]],
    segment_start: float,
    segment_end: float,
    min_seconds: int,
) -> list[dict[str, Any]]:
    useful: list[dict[str, Any]] = []
    for segment in segments:
        try:
            start = max(segment_start, float(segment["start"]))
            end = min(segment_end, float(segment["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if end <= start:
            continue
        text = clean_segment_text(str(segment.get("text", "")))
        if not text:
            continue
        if contains_host_outro_or_market_text(text):
            if useful and useful[-1]["end"] - useful[0]["start"] >= min_seconds:
                break
            continue
        normalized = dict(segment)
        normalized["start"] = start
        normalized["end"] = end
        normalized["text"] = text
        useful.append(normalized)
    return useful


def clean_segment_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def compact_title_line(text: str, max_chars: int) -> str:
    cleaned = clean_segment_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip() + "..."


def is_host_outro_or_market_recap(clip: dict[str, Any]) -> bool:
    pieces = [
        str(clip.get("title", "")),
        " ".join(str(item) for item in clip.get("title_lines", []) if item),
    ]
    for sub in clip.get("subtitles", []):
        pieces.append(str(sub.get("en", "")))
        pieces.append(str(sub.get("zh", "")))
    combined = "\n".join(pieces)
    return contains_host_outro_or_market_text(combined)


def contains_host_outro_or_market_text(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in HOST_OUTRO_PATTERNS)


if __name__ == "__main__":
    main()

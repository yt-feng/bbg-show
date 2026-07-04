#!/usr/bin/env python3
"""Render vertical bilingual highlight clips on Linux using Pillow overlays + ffmpeg.

Port of vid_cut/render_highlight_clips.py for GitHub Actions (Linux).
Uses render_overlays_pillow.py instead of Swift, libx264 instead of VideoToolbox.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wording_guard import sanitize_zh_wording  # noqa: E402


OUT_W = 1080
OUT_H = 1920
CAPTION_W = 1080
CAPTION_H = 430
COMMENT_W = 1080
COMMENT_H = 112
MAIN_Y = 690
PANEL_Y = 1110
CAPTION_Y = 1125
COMMENT_Y = 1588
SOURCE_TOP_CROP = 130
SOURCE_BOTTOM_CROP = 210
BG_DEDUPE_FILTER = "eq=brightness=-0.28:contrast=1.02:saturation=0.62:gamma=1.002,noise=alls=0.8:allf=t+u"
MAIN_DEDUPE_FILTER = "eq=brightness=0.006:contrast=1.018:saturation=1.012:gamma=1.002,unsharp=5:5:0.12"
AUDIO_DEDUPE_FILTER = (
    "highpass=f=72,lowpass=f=18500,"
    "acompressor=threshold=-20dB:ratio=1.08:attack=12:release=160,"
    "equalizer=f=3200:t=q:w=1.2:g=0.25,volume=1.012"
)
CHUNKED_SUBTITLE_THRESHOLD = 24
CHUNKED_SUBTITLES_PER_PIECE = 8
MIN_PIECE_SECONDS = 0.001
DISCLAIMER_TEXT = (
    "免责声明：本内容仅作观点与信息分享，不构成投资、保险、理财、股票、基金等建议，"
    "市场有风险，投资需谨慎；亦不构成任何证券或金融工具买卖的出价、征价或招揽。"
    "评级、目标价、估值、盈利预测等分析判断，不构成对具体证券/金融工具在具体价位、"
    "具体时点、具体市场表现的投资建议，亦不构成针对任何人的具体投资操作意见。"
    "请结合自身情况独立评估、审慎决策并自行承担风险；本账号不保证资料的准确性、"
    "可靠性、时效性及完整性，亦不构成任何合同或承诺的基础。"
    "参考资料：彭博社；部分内容由AI辅助生成。"
)
TimedOverlay = tuple[Path, float, float, int]
SENSITIVE_ZH_TERMS = [
    "投资",
    "革命",
]
PARAPHRASE_ZH_TERMS = [
    ("投资者", "市场参与者"),
    ("投资主题", "主线"),
    ("中国股票", "中国市场"),
    ("中国股", "中国市场"),
    ("A 股", "内地市场"),
    ("A股", "内地市场"),
    ("港股", "香港市场"),
    ("美股", "美国市场"),
    ("股市", "市场"),
    ("股票", "权益资产"),
    ("个股", "单家公司"),
    ("股", "权益"),
]


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Source video MP4")
    parser.add_argument("--plan", type=Path, required=True, help="highlight_plan.json")
    parser.add_argument("--out-dir", type=Path, default=Path("clips_final"))
    parser.add_argument("--work-dir", type=Path, default=Path("work/render"))
    parser.add_argument("--only", help="Render specific clip indexes (e.g. 1,2 or 1-3)")
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"Source video not found: {args.source}")
    if not args.plan.exists():
        raise SystemExit(f"Plan not found: {args.plan}")

    payload = json.loads(args.plan.read_text(encoding="utf-8"))
    clips = payload.get("clips", [])
    if not clips:
        raise SystemExit("No clips in plan")

    selected = parse_only(args.only, len(clips))
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)

    renderer = Path(__file__).with_name("render_overlays_pillow.py")
    if not renderer.exists():
        raise SystemExit(f"Pillow renderer not found: {renderer}")

    for one_based in selected:
        clip = clips[one_based - 1]
        output = args.out_dir / output_name(one_based, clip)
        if output.exists() and not args.force:
            print(f"[{one_based}/{len(clips)}] Exists, skipping: {output}", flush=True)
            continue

        clip_dir = args.work_dir / f"clip_{one_based:02d}"
        clip_dir.mkdir(parents=True, exist_ok=True)

        # Generate overlay PNGs
        static_png, timed_overlays = render_overlay_images(renderer, clip_dir, clip)

        duration = duration_of(clip)
        subtitle_count = sum(1 for item in timed_overlays if item[3] == CAPTION_Y)
        comment_count = sum(1 for item in timed_overlays if item[3] == COMMENT_Y)
        print(
            f"[{one_based}/{len(clips)}] Rendering: {clip.get('title', '')} "
            f"({duration:.1f}s, {subtitle_count} subtitles, {comment_count} comments)",
            flush=True,
        )

        # Composite with ffmpeg
        render_clip(
            source=args.source,
            clip=clip,
            static_png=static_png,
            timed_overlays=timed_overlays,
            output=output,
            threads=args.threads,
        )
        print(f"[{one_based}/{len(clips)}] Wrote: {output}", flush=True)

    print(f"\nDone. {len(selected)} clips rendered to {args.out_dir}", flush=True)


def parse_only(value: str | None, total: int) -> list[int]:
    if not value:
        return list(range(1, total + 1))
    indexes: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if "-" in part:
            left, right = part.split("-", 1)
            indexes.update(range(int(left), int(right) + 1))
        elif part:
            indexes.add(int(part))
    result = sorted(indexes)
    for idx in result:
        if idx < 1 or idx > total:
            raise SystemExit(f"--only index out of range: {idx}; plan has {total} clip(s)")
    return result


def render_overlay_images(
    renderer: Path,
    clip_dir: Path,
    clip: dict[str, Any],
) -> tuple[Path, list[TimedOverlay]]:
    """Generate overlay PNGs using the Pillow renderer."""
    static_png = clip_dir / "static.png"
    jobs: list[dict[str, Any]] = [
        {
            "kind": "static",
            "output": str(static_png),
            "width": OUT_W,
            "height": OUT_H,
            "title": safe_zh_text(str(clip.get("title", ""))),
            "titleLines": [safe_zh_text(line) for line in title_lines_for_clip(clip)],
            "titleHighlights": [safe_zh_text(str(item)) for item in clip.get("title_highlights", [])],
            "watermark": "KC桌面",
            "cta": "关注「KC桌面」，追踪国际热点",
            "disclaimer": DISCLAIMER_TEXT,
        }
    ]

    timed_overlays: list[TimedOverlay] = []
    comment_by_index = subtitle_comment_map(clip)
    for fallback_index, subtitle in enumerate(clip.get("subtitles", []), start=1):
        start, end = relative_times(clip, subtitle)
        if end <= start:
            continue
        index = subtitle_index(subtitle, fallback_index)
        zh_source = subtitle.get("zh_filtered") or subtitle.get("zh", "")
        png = clip_dir / f"sub_{index:03d}.png"
        jobs.append({
            "kind": "subtitle",
            "output": str(png),
            "width": CAPTION_W,
            "height": CAPTION_H,
            "zh": safe_zh_text(str(zh_source)),
            "en": clean_display_text(str(subtitle.get("en", ""))),
            "zhHighlights": [safe_zh_text(str(item)) for item in subtitle.get("zh_highlights", [])],
            "enHighlights": subtitle.get("en_highlights", []),
        })
        timed_overlays.append((png, start, end, CAPTION_Y))

        comment, comment_highlights = subtitle_comment_for_overlay(clip, subtitle, index, comment_by_index)
        if comment:
            comment_png = clip_dir / f"comment_{index:03d}.png"
            jobs.append({
                "kind": "comment",
                "output": str(comment_png),
                "width": COMMENT_W,
                "height": COMMENT_H,
                "comment": comment,
                "commentHighlights": comment_highlights,
            })
            timed_overlays.append((comment_png, start, end, COMMENT_Y))

    batch_path = clip_dir / "overlay_batch.json"
    batch_path.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    subprocess.run([sys.executable, str(renderer), str(batch_path)], check=True)
    return static_png, timed_overlays


def subtitle_index(subtitle: dict[str, Any], fallback: int) -> int:
    try:
        return int(subtitle.get("index", fallback))
    except (TypeError, ValueError):
        return fallback


def subtitle_comment_map(clip: dict[str, Any]) -> dict[int, tuple[str, list[str]]]:
    raw_items = clip.get("subtitle_comments", [])
    if not isinstance(raw_items, list):
        return {}

    mapped: dict[int, tuple[str, list[str]]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("subtitle_index", item.get("index", 0)))
        except (TypeError, ValueError):
            continue
        comment = normalize_kc_comment(str(item.get("comment", "")), body_max_chars=34)
        if not comment:
            continue
        highlights = item.get("comment_highlights", item.get("highlights", []))
        if not isinstance(highlights, list):
            highlights = []
        mapped[idx] = (
            safe_zh_text(comment),
            [safe_zh_text(str(highlight)) for highlight in highlights if str(highlight).strip()],
        )
    return mapped


def subtitle_comment_for_overlay(
    clip: dict[str, Any],
    subtitle: dict[str, Any],
    index: int,
    comment_by_index: dict[int, tuple[str, list[str]]],
) -> tuple[str, list[str]]:
    if index in comment_by_index:
        return comment_by_index[index]

    raw_highlights = subtitle.get("zh_highlights")
    if isinstance(raw_highlights, list):
        for raw in raw_highlights:
            key = safe_zh_text(str(raw))[:10].strip()
            if key:
                return normalize_kc_comment(f"KC评论：盯住{key}这个信号", 34), [key]

    zh = safe_zh_text(str(subtitle.get("zh_filtered") or subtitle.get("zh") or ""))
    if zh:
        phrase = zh[:12].rstrip(" ，,。；;、")
        if phrase:
            return normalize_kc_comment(f"KC评论：{phrase}是关键信号", 34), [phrase[:8]]

    clip_comment = normalize_kc_comment(str(clip.get("comment", "")), body_max_chars=34)
    if clip_comment:
        body = clip_comment.removeprefix("KC评论：")
        return clip_comment, [body[:8]] if body else []
    return "", []


def normalize_kc_comment(comment: str, body_max_chars: int) -> str:
    value = safe_zh_text(comment)
    value = re.sub(r"\s+", "", value).strip(" ，,。；;、｜|-")
    if not value:
        return ""
    if not value.startswith("KC评论："):
        value = "KC评论：" + value.removeprefix("KC评论:").removeprefix("KC点评：").removeprefix("点评：")
    prefix = "KC评论："
    body = value[len(prefix):] if value.startswith(prefix) else value
    if len(body) > body_max_chars:
        value = prefix + truncate_comment_body(body, body_max_chars)
    return value


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


def relative_times(clip: dict[str, Any], subtitle: dict[str, Any]) -> tuple[float, float]:
    if "relative_start" in subtitle and "relative_end" in subtitle:
        start = float(subtitle["relative_start"])
        end = float(subtitle["relative_end"])
    else:
        start = float(subtitle["start"]) - float(clip["start"])
        end = float(subtitle["end"]) - float(clip["start"])
    duration = duration_of(clip)
    return max(0.0, start), min(duration, end)


def render_clip(
    *,
    source: Path,
    clip: dict[str, Any],
    static_png: Path,
    timed_overlays: list[TimedOverlay],
    output: Path,
    threads: int,
) -> None:
    if len(timed_overlays) > CHUNKED_SUBTITLE_THRESHOLD:
        print(
            f"  Using chunked renderer for {len(timed_overlays)} timed overlays",
            flush=True,
        )
        render_clip_chunked(
            source=source,
            clip=clip,
            static_png=static_png,
            timed_overlays=timed_overlays,
            output=output,
            threads=threads,
        )
        return

    command = build_ffmpeg_command(
        source=source,
        clip=clip,
        static_png=static_png,
        timed_overlays=timed_overlays,
        output=output,
        threads=threads,
    )
    subprocess.run(command, check=True)


def render_clip_chunked(
    *,
    source: Path,
    clip: dict[str, Any],
    static_png: Path,
    timed_overlays: list[TimedOverlay],
    output: Path,
    threads: int,
) -> None:
    duration = duration_of(clip)
    piece_dir = static_png.parent / "pieces"
    piece_dir.mkdir(parents=True, exist_ok=True)
    pieces = build_render_pieces(duration, timed_overlays)

    segment_paths: list[Path] = []
    for idx, (start, end, piece_overlays) in enumerate(pieces, start=1):
        piece_duration = end - start
        if piece_duration < MIN_PIECE_SECONDS:
            continue
        segment_path = piece_dir / f"part_{idx:04d}.mp4"
        command = build_video_piece_command(
            source=source,
            clip=clip,
            static_png=static_png,
            timed_overlays=piece_overlays,
            piece_start=start,
            piece_duration=piece_duration,
            output=segment_path,
            threads=threads,
        )
        print(
            f"    part {idx:04d}: {start:.2f}-{end:.2f}s "
            f"{len(piece_overlays)} timed overlays",
            flush=True,
        )
        subprocess.run(command, check=True)
        segment_paths.append(segment_path)

    if not segment_paths:
        raise SystemExit("Chunked renderer produced no video segments")

    concat_list = piece_dir / "concat.txt"
    concat_video = piece_dir / "video_concat.mp4"
    audio_path = piece_dir / "audio.m4a"
    concat_list.write_text(
        "ffconcat version 1.0\n"
        + "".join(f"file '{ffconcat_escape(path.resolve())}'\n" for path in segment_paths),
        encoding="utf-8",
    )

    subprocess.run([
        "ffmpeg",
        "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-nostats",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        str(concat_video),
    ], check=True)

    subprocess.run(build_audio_command(
        source=source,
        clip=clip,
        duration=duration,
        output=audio_path,
    ), check=True)

    subprocess.run([
        "ffmpeg",
        "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-nostats",
        "-i", str(concat_video),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ], check=True)


def build_render_pieces(
    duration: float,
    timed_overlays: list[TimedOverlay],
) -> list[tuple[float, float, list[TimedOverlay]]]:
    pieces: list[tuple[float, float, list[TimedOverlay]]] = []
    cursor = 0.0
    overlays: list[TimedOverlay] = []
    for png, raw_start, raw_end, y in sorted(timed_overlays, key=lambda item: (item[1], item[2], item[3])):
        start = max(0.0, min(duration, raw_start))
        end = max(0.0, min(duration, raw_end))
        if end > start:
            overlays.append((png, start, end, y))

    if not overlays:
        return [(0.0, duration, [])]

    for offset in range(0, len(overlays), CHUNKED_SUBTITLES_PER_PIECE):
        group = overlays[offset:offset + CHUNKED_SUBTITLES_PER_PIECE]
        next_group = overlays[offset + CHUNKED_SUBTITLES_PER_PIECE:offset + CHUNKED_SUBTITLES_PER_PIECE + 1]
        start = cursor
        if next_group:
            end = max(group[-1][2], next_group[0][1])
        else:
            end = duration
        end = max(start, min(duration, end))
        if end > start:
            pieces.append((start, end, group))
            cursor = end

    if cursor < duration:
        pieces.append((cursor, duration, []))
    return pieces


def build_video_piece_command(
    *,
    source: Path,
    clip: dict[str, Any],
    static_png: Path,
    timed_overlays: list[TimedOverlay],
    piece_start: float,
    piece_duration: float,
    output: Path,
    threads: int,
) -> list[str]:
    image_inputs = [
        "-loop", "1", "-framerate", "30000/1001",
        "-t", f"{piece_duration:.3f}", "-i", str(static_png),
    ]
    for overlay_png, _, _, _ in timed_overlays:
        image_inputs.extend([
            "-loop", "1", "-framerate", "30000/1001",
            "-t", f"{piece_duration:.3f}", "-i", str(overlay_png),
        ])

    return [
        "ffmpeg",
        "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-nostats",
        "-filter_threads", str(threads),
        "-filter_complex_threads", str(threads),
        "-ss", f"{float(clip['start']) + piece_start:.3f}",
        "-t", f"{piece_duration:.3f}",
        "-i", str(source),
        *image_inputs,
        "-filter_complex", build_video_piece_filter(
            piece_start=piece_start,
            piece_duration=piece_duration,
            timed_overlays=timed_overlays,
        ),
        "-map", "[vout]",
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-threads", str(threads), "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output),
    ]


def build_video_piece_filter(
    *,
    piece_start: float,
    piece_duration: float,
    timed_overlays: list[TimedOverlay],
) -> str:
    parts = [
        "[0:v]setpts=PTS-STARTPTS,split=2[bgsrc][mainsrc]",
        (
            f"[bgsrc]crop=iw:ih-{SOURCE_TOP_CROP + SOURCE_BOTTOM_CROP}:0:{SOURCE_TOP_CROP},"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,{BG_DEDUPE_FILTER}[bg]"
        ),
        (
            f"[mainsrc]crop=iw:ih-{SOURCE_TOP_CROP + SOURCE_BOTTOM_CROP}:0:{SOURCE_TOP_CROP},"
            f"scale=1080:-2,{MAIN_DEDUPE_FILTER}[main]"
        ),
        (
            f"[bg][main]overlay=0:{MAIN_Y}:format=auto,"
            f"drawbox=x=0:y={PANEL_Y}:w=1080:h={OUT_H - PANEL_Y}:color=black@0.88:t=fill[base]"
        ),
        "[base][1:v]overlay=0:0:format=auto[v1]",
    ]

    previous = "v1"
    for input_idx, (_, start, end, y) in enumerate(timed_overlays, start=2):
        local_start = max(0.0, start - piece_start)
        local_end = min(piece_duration, end - piece_start)
        if local_end <= local_start:
            continue
        label = f"v{input_idx}"
        expr = f"between(t\\,{local_start:.3f}\\,{local_end:.3f})"
        parts.append(f"[{previous}][{input_idx}:v]overlay=0:{y}:format=auto:enable={expr}[{label}]")
        previous = label

    parts.append(f"[{previous}]format=yuv420p[vout]")
    return ";".join(parts)


def build_ffmpeg_command(
    *,
    source: Path,
    clip: dict[str, Any],
    static_png: Path,
    timed_overlays: list[TimedOverlay],
    output: Path,
    threads: int,
) -> list[str]:
    duration = duration_of(clip)
    image_inputs: list[str] = []
    for png in [static_png] + [item[0] for item in timed_overlays]:
        image_inputs.extend(["-loop", "1", "-framerate", "30000/1001", "-t", f"{duration:.3f}", "-i", str(png)])

    filter_complex = build_filter_complex(duration, timed_overlays)
    return [
        "ffmpeg",
        "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-nostats",
        "-filter_threads", str(threads),
        "-filter_complex_threads", str(threads),
        "-ss", f"{float(clip['start']):.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(source),
        *image_inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-map", "[aout]",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
        "-threads", str(threads), "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-movflags", "+faststart",
        "-shortest",
        str(output),
    ]


def build_filter_complex(duration: float, timed_overlays: list[TimedOverlay]) -> str:
    parts = [
        "[0:v]setpts=PTS-STARTPTS,split=2[bgsrc][mainsrc]",
        (
            f"[bgsrc]crop=iw:ih-{SOURCE_TOP_CROP + SOURCE_BOTTOM_CROP}:0:{SOURCE_TOP_CROP},"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            f"crop=1080:1920,{BG_DEDUPE_FILTER}[bg]"
        ),
        (
            f"[mainsrc]crop=iw:ih-{SOURCE_TOP_CROP + SOURCE_BOTTOM_CROP}:0:{SOURCE_TOP_CROP},"
            f"scale=1080:-2,{MAIN_DEDUPE_FILTER}[main]"
        ),
        (
            f"[bg][main]overlay=0:{MAIN_Y}:format=auto,"
            f"drawbox=x=0:y={PANEL_Y}:w=1080:h={OUT_H - PANEL_Y}:color=black@0.88:t=fill[base]"
        ),
        "[base][1:v]overlay=0:0:format=auto[v1]",
    ]

    previous = "v1"
    for idx, (_, start, end, y) in enumerate(timed_overlays, start=2):
        label = f"v{idx}"
        expr = f"between(t\\,{start:.3f}\\,{end:.3f})"
        parts.append(f"[{previous}][{idx}:v]overlay=0:{y}:format=auto:enable={expr}[{label}]")
        previous = label

    parts.append(f"[{previous}]format=yuv420p[vout]")

    fade_out_start = max(0.0, duration - 5.0)
    fade_out_duration = min(5.0, duration)
    parts.append(
        f"[0:a]asetpts=PTS-STARTPTS,{build_audio_filter(duration)}[aout]"
    )
    return ";".join(parts)


def build_audio_command(
    *,
    source: Path,
    clip: dict[str, Any],
    duration: float,
    output: Path,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner", "-y", "-nostdin", "-loglevel", "error", "-nostats",
        "-ss", f"{float(clip['start']):.3f}",
        "-t", f"{duration:.3f}",
        "-i", str(source),
        "-vn",
        "-af", f"asetpts=PTS-STARTPTS,{build_audio_filter(duration)}",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        str(output),
    ]


def build_audio_filter(duration: float) -> str:
    fade_out_start = max(0.0, duration - 5.0)
    fade_out_duration = min(5.0, duration)
    return (
        f"{AUDIO_DEDUPE_FILTER},"
        "afade=t=in:st=0:d=3,"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_duration:.3f}"
    )


def ffconcat_escape(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace("'", "\\'")


def title_lines_for_clip(clip: dict[str, Any]) -> list[str]:
    lines = clip.get("title_lines")
    if isinstance(lines, list) and len(lines) >= 3:
        cleaned = [
            clean_display_text(str(item)).replace("：", "").replace(":", "")
            for item in lines[:3]
        ]
        if all(cleaned):
            return cleaned
    if isinstance(lines, list) and len(lines) >= 2:
        cleaned = [clean_display_text(str(item)).replace("：", "").replace(":", "") for item in lines[:2]]
        if all(cleaned):
            return [cleaned[0], *split_title_topic(cleaned[1])]

    title = clean_display_text(str(clip.get("title", "")))
    if "：" in title:
        left, right = title.split("：", 1)
        return [left.strip(), *split_title_topic(right.strip())]
    if ":" in title:
        left, right = title.split(":", 1)
        return [left.strip(), *split_title_topic(right.strip())]
    return [title, ""]


def split_title_topic(topic: str) -> list[str]:
    topic = clean_display_text(topic)
    if len(topic) <= 9:
        return [topic, ""]

    if topic.startswith("市场已提前定价"):
        return ["市场已提前定价", topic.removeprefix("市场已提前定价").strip()]

    candidates = []
    for marker in ["预期", "央行", "回调后", "估值", "可能是", "已提前", "城市更新", "房价"]:
        idx = topic.find(marker)
        if idx > 0:
            candidates.append(idx + len(marker))

    if candidates:
        split_at = min(candidates, key=lambda pos: abs(pos - len(topic) / 2))
    else:
        split_at = len(topic) // 2
        for pos in range(max(4, split_at - 4), min(len(topic) - 3, split_at + 5)):
            left = topic[:pos]
            right = topic[pos:]
            if not left.endswith(("的", "是", "和", "与")) and not right.startswith(("的", "是", "和", "与")):
                split_at = pos
                break

    return [topic[:split_at].strip(), topic[split_at:].strip()]


def output_name(index: int, clip: dict[str, Any]) -> str:
    title = safe_zh_text(str(clip.get("title", f"clip_{index:02d}")))
    title = title.replace("：", "_").replace(":", "_")
    title = title.replace("（", "(").replace("）", ")")
    title = re.sub(r"\((\d+)/(\d+)\)", r"_\1-\2", title)
    title = re.sub(r"[\\/:*?\"<>|]", "_", title)
    title = re.sub(r"\s+", "_", title).strip("._ ")
    title = re.sub(r"_+", "_", title)
    return f"{index:02d}_{title}.mp4"


def duration_of(clip: dict[str, Any]) -> float:
    return float(clip["end"]) - float(clip["start"])


def filter_sensitive_zh(text: str) -> str:
    for term in sorted(SENSITIVE_ZH_TERMS, key=len, reverse=True):
        text = text.replace(term, "**")
    return text


def paraphrase_sensitive_zh(text: str) -> str:
    for old, new in PARAPHRASE_ZH_TERMS:
        text = text.replace(old, new)
    return sanitize_zh_wording(text)


def safe_zh_text(text: str) -> str:
    return filter_sensitive_zh(paraphrase_sensitive_zh(clean_display_text(text)))


def clean_display_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    fixes = {
        "straighter hummus": "Strait of Hormuz",
        "street of Ramuz": "Strait of Hormuz",
        "strait of hummus": "Strait of Hormuz",
        "Richard Minman": "Richard Koo",
    }
    for bad, good in fixes.items():
        text = re.sub(re.escape(bad), good, text, flags=re.IGNORECASE)
    return text


if __name__ == "__main__":
    main()

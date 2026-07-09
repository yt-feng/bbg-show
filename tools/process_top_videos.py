#!/usr/bin/env python3
"""Download, transcribe, plan, and render Bloomberg Top Videos."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_bloomberg_video import safe_file_part, slug_from_url  # noqa: E402
from trump_filter import is_trump_related, remove_trump_clips_from_plan  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent


def run_date_default() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def run(command: list[str], env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(command), flush=True)
    started = time.monotonic()
    subprocess.run(command, check=True, env=env)
    elapsed = time.monotonic() - started
    print(f"Finished in {elapsed:.1f}s: {' '.join(command[:2])}", flush=True)


def ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    return float(proc.stdout.strip())


def load_manifest(path: Path, max_videos: int) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    videos = data.get("videos", [])
    if not isinstance(videos, list) or not videos:
        raise SystemExit(f"No videos found in manifest: {path}")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in videos:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url or url in seen:
            continue
        title = str(item.get("title", "")).strip() or slug_from_url(url).replace("_", " ").title()
        slug = str(item.get("slug", "")).strip() or slug_from_url(url)
        if is_trump_related(url, title, slug, use_ai=True):
            print(f"[top-videos] Skipping sensitive-topic manifest video: {title or url}", flush=True)
            continue
        seen.add(url)
        normalized.append({
            "url": url,
            "title": title,
            "slug": slug,
        })
        if len(normalized) >= max_videos:
            break
    if not normalized:
        raise SystemExit(f"No valid video URLs found in manifest: {path}")
    return normalized


def process_one(
    item: dict[str, str],
    index: int,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    url = item["url"]
    title = item["title"]
    slug = safe_file_part(item.get("slug") or slug_from_url(url))
    label = f"{index:02d}_{slug}"
    work_dir = args.work_root / label
    video_path = work_dir / f"{label}.mp4"
    transcript_path = work_dir / "transcript.json"
    plan_path = work_dir / "highlight_plan.json"
    render_dir = output_dir / label

    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== Top video {index}: {title} ===", flush=True)
    print(url, flush=True)

    print(f"[top-video {index:02d}] Downloading source video", flush=True)
    run([
        sys.executable,
        str(TOOLS / "download_bloomberg_video.py"),
        "--url", url,
        "--download-backend", args.download_backend,
        "--yt-dlp-proxy-mode", "auto",
        "--output", str(video_path),
        "--work-dir", str(work_dir / "download"),
        "--workers", str(args.workers),
        "--no-strategy-cache",
        "--force",
    ])

    duration = ffprobe_duration(video_path)
    if duration < args.min_video_seconds:
        raise RuntimeError(f"Downloaded video is too short: {duration:.1f}s")
    print(f"[top-video {index:02d}] Downloaded {duration:.1f}s source: {video_path}", flush=True)

    print(f"[top-video {index:02d}] Transcribing source video", flush=True)
    run([
        sys.executable,
        str(TOOLS / "transcribe_video.py"),
        "--video", str(video_path),
        "--out", str(transcript_path),
        "--model", args.whisper_model,
        "--language", "en",
        "--force",
    ])

    print(
        f"[top-video {index:02d}] Planning translated clip "
        f"(max {args.max_clip_seconds:.0f}s) with DeepSeek",
        flush=True,
    )
    run([
        sys.executable,
        str(TOOLS / "plan_top_video_full.py"),
        "--transcript", str(transcript_path),
        "--source-url", url,
        "--source-title", title,
        "--segment-start", "0",
        "--segment-end", f"{duration:.2f}",
        "--max-clip-seconds", f"{args.max_clip_seconds:.2f}",
        "--out", str(plan_path),
    ])

    print(f"[top-video {index:02d}] Refining title with DeepSeek", flush=True)
    run([
        sys.executable,
        str(TOOLS / "refine_clip_titles.py"),
        "--plan", str(plan_path),
    ])
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    removed = remove_trump_clips_from_plan(plan, use_ai=True)
    if removed:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[top-video {index:02d}] Removed {len(removed)} sensitive-topic clip(s)", flush=True)
    if not plan.get("clips"):
        raise RuntimeError("Top video skipped by sensitive topic filter")

    print(f"[top-video {index:02d}] Rendering KC Desktop clip", flush=True)
    if render_dir.exists():
        shutil.rmtree(render_dir)
    render_dir.mkdir(parents=True, exist_ok=True)
    run([
        sys.executable,
        str(TOOLS / "render_clips_linux.py"),
        "--source", str(video_path),
        "--plan", str(plan_path),
        "--out-dir", str(render_dir),
        "--work-dir", str(work_dir / "render"),
        "--threads", str(args.threads),
        "--force",
    ])

    rendered = sorted(path for path in render_dir.glob("*.mp4"))
    if not rendered:
        raise RuntimeError("Renderer produced no MP4 files")

    first_clip = next(iter(plan.get("clips", [])), {})
    refined_title = str(first_clip.get("title", "")).strip()
    metadata = {
        "index": index,
        "url": url,
        "title": refined_title or title,
        "source_title": title,
        "slug": slug,
        "duration": round(duration, 2),
        "max_clip_seconds": args.max_clip_seconds,
        "video_file": str(video_path),
        "transcript": str(transcript_path),
        "highlight_plan": "highlight_plan.json",
        "rendered_files": [path.name for path in rendered],
    }
    (render_dir / "video.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(plan_path, render_dir / "highlight_plan.json")
    title_log = plan_path.with_name("title_refine_log.json")
    if title_log.exists():
        shutil.copy2(title_log, render_dir / "title_refine_log.json")
    return {
        "status": "success",
        **metadata,
        "output_dir": str(render_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-date", default="", help="YYYY-MM-DD, defaults to today in Asia/Shanghai.")
    parser.add_argument("--max-videos", type=int, default=9)
    parser.add_argument("--video-index", type=int, default=0, help="Process only this 1-based manifest index.")
    parser.add_argument("--out-root", type=Path, default=ROOT / "rendered-clips" / "top-videos")
    parser.add_argument("--work-root", type=Path, default=ROOT / "work" / "top-videos")
    parser.add_argument("--download-backend", choices=("auto", "yt-dlp", "custom"), default="auto")
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--min-video-seconds", type=float, default=15.0)
    parser.add_argument("--max-clip-seconds", type=float, default=90.0)
    parser.add_argument("--no-copy-manifest", action="store_true")
    parser.add_argument("--clean-output-dir", action="store_true")
    args = parser.parse_args()

    run_date = args.run_date.strip() or run_date_default()
    output_dir = args.out_root / run_date
    if args.clean_output_dir and output_dir.exists():
        print(f"Cleaning output directory before processing: {output_dir}", flush=True)
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)
    (args.work_root / "output_dir.txt").write_text(str(output_dir) + "\n", encoding="utf-8")

    videos = load_manifest(args.manifest, args.max_videos)
    if args.video_index:
        if args.video_index < 1 or args.video_index > len(videos):
            raise SystemExit(f"--video-index {args.video_index} out of range; manifest has {len(videos)} video(s)")
        selected_videos = [(args.video_index, videos[args.video_index - 1])]
    else:
        selected_videos = list(enumerate(videos, start=1))
    if not args.no_copy_manifest:
        shutil.copy2(args.manifest, output_dir / "top_videos.json")

    results: list[dict[str, Any]] = []
    for index, item in selected_videos:
        try:
            result = process_one(item, index, args, output_dir)
        except Exception as exc:  # noqa: BLE001 - keep the daily batch moving
            message = str(exc)
            topic_skip = (
                "Trump filter" in message
                or "Trump-related" in message
                or "sensitive topic filter" in message
                or "sensitive-topic" in message
            )
            status = "skipped" if topic_skip else "failed"
            print(f"{status.upper()} top video {index}: {exc}", flush=True)
            result = {
                "status": status,
                "index": index,
                "url": item.get("url", ""),
                "title": item.get("title", ""),
                "error": str(exc),
            }
            if topic_skip:
                result["skip_reason"] = "sensitive_topic"
        results.append(result)

    summary = {
        "run_date": run_date,
        "source_manifest": str(args.manifest),
        "output_dir": str(output_dir),
        "video_index": args.video_index or None,
        "total": len(results),
        "succeeded": sum(1 for item in results if item.get("status") == "success"),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "skipped": sum(1 for item in results if item.get("status") == "skipped"),
        "videos": results,
    }
    summary_name = f"summary_{args.video_index:02d}.json" if args.video_index else "summary.json"
    (output_dir / summary_name).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if summary["succeeded"] < 1 and summary["skipped"] < summary["total"]:
        raise SystemExit("No Bloomberg Top Videos were processed successfully")


if __name__ == "__main__":
    main()

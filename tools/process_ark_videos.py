#!/usr/bin/env python3
"""Download, transcribe, plan, and render ARK Invest Cathie Wood videos."""

from __future__ import annotations

import argparse
import json
import os
import re
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


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def ytdlp_command() -> list[str]:
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    return [sys.executable, "-m", "yt_dlp"]


def load_manifest(path: Path, max_videos: int) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    videos = payload.get("videos", [])
    if not isinstance(videos, list):
        raise SystemExit(f"Invalid manifest videos field: {path}")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in videos:
        if not isinstance(item, dict):
            continue
        url = clean_text(str(item.get("url", "")))
        title = clean_text(str(item.get("title") or item.get("source_title") or ""))
        if not url or not title or url in seen:
            continue
        seen.add(url)
        slug = clean_text(str(item.get("slug", ""))) or safe_file_part(slug_from_url(url) or title)
        item = {
            **item,
            "url": url,
            "title": title,
            "source_title": clean_text(str(item.get("source_title") or title)),
            "slug": safe_file_part(slug) or f"ark-video-{len(normalized) + 1}",
            "speaker": clean_text(str(item.get("speaker") or "Cathie Wood")),
            "download_query": clean_text(str(item.get("download_query") or f"{title} ARK Invest Cathie Wood")),
        }
        normalized.append(item)
        if len(normalized) >= max_videos:
            break
    return normalized


def parse_json_lines(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        entries = item.get("entries")
        if isinstance(entries, list):
            items.extend(entry for entry in entries if isinstance(entry, dict))
        else:
            items.append(item)
    return items


def word_set(value: str) -> set[str]:
    return {
        word.casefold()
        for word in re.findall(r"[A-Za-z0-9]+", value)
        if len(word) > 1
    }


def candidate_score(candidate: dict[str, Any], source_title: str) -> int:
    title = clean_text(str(candidate.get("title", "")))
    channel = clean_text(str(candidate.get("channel") or candidate.get("uploader") or ""))
    url = clean_text(str(candidate.get("webpage_url") or candidate.get("url") or ""))
    haystack = f"{title} {channel} {url}".casefold()
    source_words = word_set(source_title)
    title_words = word_set(title)
    score = len(source_words & title_words)
    if "ark invest" in haystack or "arkinvest" in haystack:
        score += 10
    if "cathie" in haystack:
        score += 4
    if "wood" in haystack:
        score += 3
    if "in the know" in haystack or "itk" in haystack:
        score += 3
    return score


def resolve_youtube_url(item: dict[str, Any], work_dir: Path, max_search_results: int) -> dict[str, Any]:
    query = item["download_query"]
    search_url = f"ytsearch{max_search_results}:{query}"
    command = [
        *ytdlp_command(),
        "--dump-json",
        "--skip-download",
        "--no-warnings",
        "--no-playlist",
        search_url,
    ]
    print(f"[ark] Resolving YouTube source: {query}", flush=True)
    proc = subprocess.run(command, text=True, capture_output=True)
    (work_dir / "yt_dlp_search.stdout").write_text(proc.stdout, encoding="utf-8")
    (work_dir / "yt_dlp_search.stderr").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp search failed: {proc.stderr.strip()[:500]}")

    candidates = parse_json_lines(proc.stdout)
    if not candidates:
        raise RuntimeError("yt-dlp search returned no candidates")
    candidates.sort(key=lambda candidate: candidate_score(candidate, item["title"]), reverse=True)
    selected = candidates[0]
    (work_dir / "yt_dlp_selected.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    webpage_url = clean_text(str(selected.get("webpage_url") or selected.get("url") or ""))
    if not webpage_url:
        raise RuntimeError("Selected yt-dlp candidate had no URL")
    print(
        "[ark] Selected source: "
        + clean_text(str(selected.get("title", "")))
        + " / "
        + clean_text(str(selected.get("channel") or selected.get("uploader") or ""))
        + " / "
        + webpage_url,
        flush=True,
    )
    return {
        "url": webpage_url,
        "title": clean_text(str(selected.get("title", ""))),
        "channel": clean_text(str(selected.get("channel") or selected.get("uploader") or "")),
        "duration": selected.get("duration"),
        "score": candidate_score(selected, item["title"]),
    }


def download_video(source_url: str, output: Path, work_dir: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        *ytdlp_command(),
        "--no-playlist",
        "--retries", "5",
        "--fragment-retries", "5",
        "--merge-output-format", "mp4",
        "-f", "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best",
        "-S", "res:1080,ext:mp4:m4a",
        "--force-overwrites",
        "--newline",
        "-o", str(output),
        source_url,
    ]
    (work_dir / "yt_dlp_download_command.json").write_text(
        json.dumps(command, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    run(command)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": []}
    if not isinstance(payload, dict):
        return {"processed": []}
    if not isinstance(payload.get("processed"), list):
        payload["processed"] = []
    return payload


def update_state(path: Path, successes: list[dict[str, Any]]) -> None:
    if not successes:
        return
    state = load_state(path)
    existing: dict[str, dict[str, Any]] = {}
    for item in state.get("processed", []):
        if not isinstance(item, dict):
            continue
        key = clean_text(str(item.get("url") or item.get("guid") or ""))
        if key:
            existing[key] = item

    processed_at = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()
    for result in successes:
        key = clean_text(str(result.get("url") or result.get("guid") or ""))
        if not key:
            continue
        existing[key] = {
            "url": result.get("url", ""),
            "guid": result.get("guid", ""),
            "title": result.get("source_title") or result.get("title", ""),
            "pub_date": result.get("pub_date", ""),
            "youtube_url": result.get("youtube_url", ""),
            "output_dir": result.get("output_dir", ""),
            "rendered_files": result.get("rendered_files", []),
            "processed_at": processed_at,
        }

    payload = {
        "updated_at": processed_at,
        "processed": sorted(existing.values(), key=lambda row: str(row.get("pub_date", "")), reverse=True),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def process_one(
    item: dict[str, Any],
    index: int,
    args: argparse.Namespace,
    output_dir: Path,
) -> dict[str, Any]:
    url = item["url"]
    title = item["title"]
    slug = safe_file_part(str(item.get("slug") or slug_from_url(url) or title))
    label = f"{index:02d}_{slug}"
    work_dir = args.work_root / label
    video_path = work_dir / f"{label}.mp4"
    transcript_path = work_dir / "transcript.json"
    plan_path = work_dir / "highlight_plan.json"
    render_dir = output_dir / "ark-invest" / label

    work_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n=== ARK video {index}: {title} ===", flush=True)
    print(url, flush=True)

    selected_source = resolve_youtube_url(item, work_dir, args.search_results)
    download_video(selected_source["url"], video_path, work_dir)

    duration = ffprobe_duration(video_path)
    if duration < args.min_video_seconds:
        raise RuntimeError(f"Downloaded video is too short: {duration:.1f}s")
    print(f"[ark {index:02d}] Downloaded {duration:.1f}s source: {video_path}", flush=True)

    print(f"[ark {index:02d}] Transcribing source video", flush=True)
    run([
        sys.executable,
        str(TOOLS / "transcribe_video.py"),
        "--video", str(video_path),
        "--out", str(transcript_path),
        "--model", args.whisper_model,
        "--language", "en",
        "--force",
    ])

    print(f"[ark {index:02d}] Planning translated clip with DeepSeek", flush=True)
    run([
        sys.executable,
        str(TOOLS / "plan_top_video_full.py"),
        "--transcript", str(transcript_path),
        "--source-url", url,
        "--source-title", title,
        "--source-label", "ARK Invest",
        "--speaker", str(item.get("speaker") or "Cathie Wood"),
        "--segment-start", "0",
        "--segment-end", f"{duration:.2f}",
        "--max-clip-seconds", f"{args.max_clip_seconds:.2f}",
        "--out", str(plan_path),
    ])

    print(f"[ark {index:02d}] Refining title and KC comments with DeepSeek", flush=True)
    run([
        sys.executable,
        str(TOOLS / "refine_clip_titles.py"),
        "--plan", str(plan_path),
    ])

    print(f"[ark {index:02d}] Rendering KC Desktop clip", flush=True)
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

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    first_clip = next(iter(plan.get("clips", [])), {})
    refined_title = clean_text(str(first_clip.get("title", "")))
    metadata = {
        "index": index,
        "source": "ark-invest",
        "url": url,
        "guid": item.get("guid", ""),
        "title": refined_title or title,
        "source_title": title,
        "description": item.get("description", ""),
        "category": item.get("category", ""),
        "pub_date": item.get("pub_date", ""),
        "slug": slug,
        "speaker": item.get("speaker", "Cathie Wood"),
        "youtube_url": selected_source["url"],
        "youtube_title": selected_source.get("title", ""),
        "youtube_channel": selected_source.get("channel", ""),
        "duration": round(duration, 2),
        "max_clip_seconds": args.max_clip_seconds,
        "video_file": str(video_path),
        "transcript": str(transcript_path),
        "highlight_plan": "highlight_plan.json",
        "rendered_files": [path.name for path in rendered],
    }
    (render_dir / "video.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(plan_path, render_dir / "highlight_plan.json")
    return {
        "status": "success",
        **metadata,
        "output_dir": str(render_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run-date", default="", help="YYYY-MM-DD, defaults to today in Asia/Shanghai.")
    parser.add_argument("--max-videos", type=int, default=1)
    parser.add_argument("--out-root", type=Path, default=ROOT / "rendered-clips")
    parser.add_argument("--work-root", type=Path, default=ROOT / "work" / "ark-invest")
    parser.add_argument("--state", type=Path, default=ROOT / "rendered-clips" / "ark-invest" / "processed_urls.json")
    parser.add_argument("--search-results", type=int, default=5)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--min-video-seconds", type=float, default=30.0)
    parser.add_argument("--max-clip-seconds", type=float, default=110.0)
    args = parser.parse_args()

    if args.max_videos < 1:
        raise SystemExit("--max-videos must be at least 1")
    if args.search_results < 1:
        raise SystemExit("--search-results must be at least 1")

    run_date = args.run_date.strip() or run_date_default()
    output_dir = args.out_root / run_date
    output_dir.mkdir(parents=True, exist_ok=True)
    args.work_root.mkdir(parents=True, exist_ok=True)

    videos = load_manifest(args.manifest, args.max_videos)
    ark_output_dir = output_dir / "ark-invest"
    ark_output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.manifest, ark_output_dir / "ark_videos.json")

    results: list[dict[str, Any]] = []
    for index, item in enumerate(videos, start=1):
        try:
            result = process_one(item, index, args, output_dir)
        except Exception as exc:  # noqa: BLE001 - keep diagnostics in summary
            print(f"FAILED ARK video {index}: {exc}", flush=True)
            result = {
                "status": "failed",
                "index": index,
                "source": "ark-invest",
                "url": item.get("url", ""),
                "guid": item.get("guid", ""),
                "title": item.get("title", ""),
                "pub_date": item.get("pub_date", ""),
                "error": str(exc),
            }
        results.append(result)

    successes = [item for item in results if item.get("status") == "success"]
    update_state(args.state, successes)

    summary = {
        "run_date": run_date,
        "source_manifest": str(args.manifest),
        "output_dir": str(ark_output_dir),
        "state": str(args.state),
        "total": len(results),
        "succeeded": len(successes),
        "failed": sum(1 for item in results if item.get("status") == "failed"),
        "videos": results,
    }
    (ark_output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if videos and not successes:
        raise SystemExit("No ARK Invest videos were processed successfully")


if __name__ == "__main__":
    main()

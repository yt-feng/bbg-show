#!/usr/bin/env python3
"""Download, transcribe, plan, and render ARK Invest Cathie Wood videos."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_bloomberg_video import (  # noqa: E402
    BROWSER_UA,
    DEFAULT_PROXY_CACHE,
    DEFAULT_PROXY_TEST_URL,
    DEFAULT_SUBSCRIPTION,
    DEFAULT_SUBSCRIPTION_URL_FILE,
    FetchError,
    LocalProxyServer,
    cached_proxy,
    chrome_binary,
    ensure_subscription,
    fetch_text_direct,
    safe_file_part,
    scan_working_proxy,
    slug_from_url,
)
import proxy_hls_downloader as hls_downloader  # noqa: E402
from title_refinement_status import read_title_refinement_status  # noqa: E402
from trump_filter import is_trump_related, remove_trump_clips_from_plan  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
SENSITIVE_SKIP_MARKERS = (
    "source video skipped by sensitive topic filter",
    "no non-sensitive-topic clips remained after filtering",
    "no non-sensitive-topic clips found in plan",
    "no non-sensitive-topic clips remained after title refinement",
)
WISTIA_MEDIA_ID_PATTERNS = (
    re.compile(r"fast\.wistia\.(?:com|net)/embed/(?:medias|iframe)/([a-z0-9]+)", re.IGNORECASE),
    re.compile(r"\bwistia_async_([a-z0-9]+)\b", re.IGNORECASE),
    re.compile(r"\bwistia-([a-z0-9]+)-\d+\b", re.IGNORECASE),
)
WISTIA_VIDEO_TYPES = {
    "iphone_video",
    "mp4_video",
    "md_mp4_video",
    "hd_mp4_video",
}
WISTIA_IDENTITY_STOPWORDS = {
    "a",
    "an",
    "and",
    "ark",
    "cathie",
    "invest",
    "market",
    "of",
    "the",
    "to",
    "video",
    "with",
    "wood",
}
MIN_WISTIA_BINDING_SCORE = 8
MIN_WISTIA_IDENTITY_MARGIN = 8


def run_date_default() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()


def is_sensitive_skip_output(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SENSITIVE_SKIP_MARKERS)


def run(
    command: list[str],
    env: dict[str, str] | None = None,
    *,
    detect_sensitive_skip: bool = False,
) -> None:
    print("+ " + " ".join(command), flush=True)
    started = time.monotonic()
    if detect_sensitive_skip:
        proc = subprocess.run(
            command,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if proc.stdout:
            print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
        if proc.returncode:
            if is_sensitive_skip_output(proc.stdout or ""):
                raise RuntimeError("ARK video skipped by sensitive topic filter")
            raise subprocess.CalledProcessError(proc.returncode, command, output=proc.stdout)
    else:
        subprocess.run(command, check=True, env=env)
    elapsed = time.monotonic() - started
    print(f"Finished in {elapsed:.1f}s: {' '.join(command[:2])}", flush=True)


def refine_title_or_keep_planner_title(plan_path: Path) -> str:
    """Run title refinement and keep a publishable planner plan on technical failure."""
    original_plan = plan_path.read_text(encoding="utf-8")
    try:
        run(
            [
                sys.executable,
                str(TOOLS / "refine_clip_titles.py"),
                "--plan", str(plan_path),
            ],
            detect_sensitive_skip=True,
        )
    except subprocess.CalledProcessError as exc:
        plan_path.write_text(original_plan, encoding="utf-8")
        print(
            f"::warning::Title refinement failed with exit code {exc.returncode}; "
            "using the planner-generated title after content filtering.",
            flush=True,
        )
        return "planner_fallback"
    try:
        return read_title_refinement_status(plan_path)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        plan_path.write_text(original_plan, encoding="utf-8")
        print(
            f"::warning::Title refiner returned no valid status ({exc}); "
            "using the planner-generated title.",
            flush=True,
        )
        return "planner_fallback"


def ffprobe_duration(path: Path) -> float:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_type:format=duration",
            "-of", "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    try:
        payload = json.loads(proc.stdout)
        streams = payload.get("streams", [])
        duration = float(payload.get("format", {}).get("duration"))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError(f"ffprobe returned invalid media metadata for {path}") from exc
    if not any(isinstance(stream, dict) and stream.get("codec_type") == "video" for stream in streams):
        raise RuntimeError(f"Downloaded source has no video stream: {path}")
    if duration <= 0:
        raise RuntimeError(f"Downloaded source has invalid duration: {path}")
    return duration


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def ytdlp_command() -> list[str]:
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]
    return [sys.executable, "-m", "yt_dlp"]


def ytdlp_js_runtime_args(runtime: str) -> list[str]:
    runtime = clean_text(runtime)
    return ["--js-runtimes", runtime] if runtime else []


def ytdlp_pot_provider_args(base_url: str) -> list[str]:
    base_url = clean_text(base_url).rstrip("/")
    if not base_url:
        return []
    return [
        "--extractor-args",
        f"youtubepot-bgutilhttp:base_url={base_url}",
    ]


def extract_wistia_media_ids(text: str) -> list[str]:
    media_ids: list[str] = []
    for pattern in WISTIA_MEDIA_ID_PATTERNS:
        for match in pattern.finditer(text or ""):
            media_id = match.group(1).lower()
            if media_id not in media_ids:
                media_ids.append(media_id)
    return media_ids


def extract_wistia_media_id(text: str) -> str:
    media_ids = extract_wistia_media_ids(text)
    return media_ids[0] if media_ids else ""


def fetch_ark_page_with_headless_chrome(
    page_url: str,
    work_dir: Path,
    configured_chrome: str = "",
) -> str:
    browser_bin = clean_text(configured_chrome)
    if not browser_bin:
        try:
            browser_bin = chrome_binary()
        except SystemExit as exc:
            raise RuntimeError(str(exc)) from exc

    profile_dir = work_dir / "ark_headless_profile"
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        browser_bin,
        "--headless=new",
        "--disable-gpu",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-extensions",
        "--disable-sync",
        "--disable-translate",
        "--hide-scrollbars",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        f"--user-data-dir={profile_dir}",
        f"--user-agent={BROWSER_UA}",
        "--window-size=1280,720",
        "--timeout=30000",
        "--virtual-time-budget=15000",
        "--dump-dom",
        page_url,
    ]
    try:
        proc = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=75,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Headless Chrome could not load the ARK page: {exc}") from exc
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = clean_text(proc.stderr or proc.stdout)[-800:]
        raise RuntimeError(f"Headless Chrome could not load the ARK page: {detail or proc.returncode}")
    return proc.stdout


def discover_ark_wistia_media_ids(
    page_url: str,
    work_dir: Path,
    configured_chrome: str = "",
) -> list[str]:
    host = urlsplit(page_url).netloc.casefold()
    if not (host == "ark-invest.com" or host.endswith(".ark-invest.com")):
        raise RuntimeError(f"Not an ARK Invest page URL: {page_url}")

    errors: list[str] = []
    discovered: list[str] = []
    sources = [
        ("direct", page_url, "ark_page_direct.html"),
        ("reader", f"https://r.jina.ai/{page_url}", "ark_page_reader.txt"),
    ]
    for label, fetch_url, diagnostic_name in sources:
        try:
            page_text = fetch_text_direct(fetch_url, timeout=45)
        except FetchError as exc:
            errors.append(f"{label}: {exc}")
            continue
        (work_dir / diagnostic_name).write_text(page_text, encoding="utf-8")
        media_ids = extract_wistia_media_ids(page_text)
        if media_ids:
            print(
                f"[ark] Found official Wistia media candidate(s) {', '.join(media_ids)} "
                f"via {label} page fetch",
                flush=True,
            )
            for media_id in media_ids:
                if media_id not in discovered:
                    discovered.append(media_id)
        else:
            errors.append(f"{label}: no Wistia media ID")

    if discovered:
        return discovered

    try:
        page_text = fetch_ark_page_with_headless_chrome(page_url, work_dir, configured_chrome)
    except RuntimeError as exc:
        errors.append(f"headless: {exc}")
    else:
        (work_dir / "ark_page_headless.html").write_text(page_text, encoding="utf-8")
        media_ids = extract_wistia_media_ids(page_text)
        if media_ids:
            print(
                f"[ark] Found official Wistia media candidate(s) {', '.join(media_ids)} "
                "via headless Chrome",
                flush=True,
            )
            return media_ids
        errors.append("headless: no Wistia media ID")

    raise RuntimeError("Could not discover official ARK Wistia media: " + "; ".join(errors))


def discover_ark_wistia_media_id(
    page_url: str,
    work_dir: Path,
    configured_chrome: str = "",
) -> str:
    return discover_ark_wistia_media_ids(page_url, work_dir, configured_chrome)[0]


def wistia_asset_candidates(media: dict[str, Any], max_height: int) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for asset in media.get("assets", []):
        if not isinstance(asset, dict):
            continue
        asset_type = clean_text(str(asset.get("type", ""))).casefold()
        url = clean_text(str(asset.get("url", "")))
        try:
            height = int(asset.get("height") or 0)
        except (TypeError, ValueError):
            height = 0
        if asset_type not in WISTIA_VIDEO_TYPES or not url.startswith("https://"):
            continue
        if not height or height > max_height:
            continue
        if asset.get("public") is False or asset.get("status") not in (None, 2):
            continue
        candidates.append(asset)
    return sorted(
        candidates,
        key=lambda asset: (
            int(asset.get("height") or 0),
            int(asset.get("bitrate") or 0),
            int(asset.get("size") or 0),
        ),
        reverse=True,
    )


def item_datetime(item: dict[str, Any]) -> datetime | None:
    raw = clean_text(str(item.get("pub_date") or item.get("updated") or ""))
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def wistia_identity_score(media: dict[str, Any], item: dict[str, Any]) -> int:
    item_text = " ".join(
        clean_text(str(item.get(key, "")))
        for key in ("title", "source_title", "url", "slug", "description", "category")
    )
    media_text = " ".join(
        clean_text(str(media.get(key, "")))
        for key in ("name", "seoDescription", "description")
    )
    score = len(word_set(item_text) & word_set(media_text)) * 4
    combined_item = item_text.casefold()
    combined_media = media_text.casefold()
    if ("in the know" in combined_item or "itk" in word_set(item_text)) and (
        "in the know" in combined_media or "itk" in word_set(media_text)
    ):
        score += 8

    published = item_datetime(item)
    try:
        created = datetime.fromtimestamp(float(media.get("createdAt")), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        created = None
    if published is not None and created is not None:
        delta_days = abs((published.astimezone(timezone.utc) - created).total_seconds()) / 86400
        if delta_days <= 3:
            score += 30
        elif delta_days <= 14:
            score += 15
        elif delta_days <= 45:
            score += 5
        else:
            score -= min(30, int(delta_days // 30))
    return score


def wistia_binding_score(media: dict[str, Any], item: dict[str, Any]) -> int:
    item_text = " ".join(
        clean_text(str(item.get(key, "")))
        for key in ("title", "source_title", "url", "slug", "description", "category")
    )
    media_text = " ".join(
        clean_text(str(media.get(key, "")))
        for key in ("name", "seoDescription", "description")
    )
    item_words = word_set(item_text)
    media_words = word_set(media_text)
    meaningful_overlap = (item_words & media_words) - WISTIA_IDENTITY_STOPWORDS
    score = len(meaningful_overlap) * 4
    item_folded = item_text.casefold()
    media_folded = media_text.casefold()
    if ("in the know" in item_folded or "itk" in item_words) and (
        "in the know" in media_folded or "itk" in media_words
    ):
        score += 8
    return score


def fetch_wistia_media(media_id: str, work_dir: Path) -> tuple[dict[str, Any], str, str]:
    metadata_url = f"https://fast.wistia.com/embed/medias/{media_id}.json"
    metadata_text = fetch_text_direct(metadata_url, timeout=45)
    try:
        payload = json.loads(metadata_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid Wistia metadata for {media_id}: {exc}") from exc
    media = payload.get("media")
    if not isinstance(media, dict):
        raise RuntimeError(f"Wistia metadata for {media_id} had no media object")
    if clean_text(str(media.get("hashedId", ""))).casefold() != media_id.casefold():
        raise RuntimeError(f"Wistia metadata identity mismatch for {media_id}")
    if media.get("protected") is True:
        raise RuntimeError(f"Wistia media {media_id} is protected")
    (work_dir / f"wistia_media_{media_id}.json").write_text(metadata_text, encoding="utf-8")
    return media, metadata_text, metadata_url


def resolve_wistia_source(
    item: dict[str, Any],
    work_dir: Path,
    configured_chrome: str = "",
    max_height: int = 720,
) -> dict[str, Any]:
    page_url = clean_text(str(item.get("url", "")))
    media_ids = discover_ark_wistia_media_ids(page_url, work_dir, configured_chrome)
    scored_media: list[tuple[int, int, str, dict[str, Any], str, str]] = []
    metadata_errors: list[str] = []
    for media_id in media_ids:
        try:
            media, metadata_text, metadata_url = fetch_wistia_media(media_id, work_dir)
        except (FetchError, RuntimeError) as exc:
            metadata_errors.append(f"{media_id}: {exc}")
            continue
        scored_media.append((
            wistia_identity_score(media, item),
            wistia_binding_score(media, item),
            media_id,
            media,
            metadata_text,
            metadata_url,
        ))
    if not scored_media:
        raise RuntimeError("No valid Wistia metadata matched the ARK page: " + "; ".join(metadata_errors))
    scored_media.sort(key=lambda row: row[0], reverse=True)
    if scored_media[0][1] < MIN_WISTIA_BINDING_SCORE:
        raise RuntimeError(
            "Wistia media had no strong title/series binding to the ARK RSS item: "
            f"{scored_media[0][2]} (binding={scored_media[0][1]})"
        )
    if (
        len(scored_media) > 1
        and scored_media[0][0] - scored_media[1][0] < MIN_WISTIA_IDENTITY_MARGIN
    ):
        contenders = ", ".join(
            f"{row[2]} (score={row[0]}, binding={row[1]})" for row in scored_media[:3]
        )
        raise RuntimeError(f"Ambiguous Wistia media identity for ARK page: {contenders}")
    identity_score, binding_score, media_id, media, metadata_text, metadata_url = scored_media[0]
    (work_dir / "wistia_media.json").write_text(metadata_text, encoding="utf-8")

    assets = wistia_asset_candidates(media, max_height)
    asset = assets[0] if assets else {}
    asset_url = clean_text(str(asset.get("url", "")))
    hls_url = f"https://fast.wistia.com/embed/medias/{media_id}.m3u8"
    if not asset_url and not media.get("hls_enabled"):
        raise RuntimeError(f"Wistia media {media_id} had no downloadable video asset")

    source = {
        "provider": "wistia",
        "media_id": media_id,
        "identity_score": identity_score,
        "binding_score": binding_score,
        "url": asset_url or hls_url,
        "asset_url": asset_url,
        "hls_url": hls_url,
        "caption_url": f"https://fast.wistia.net/embed/captions/{media_id}.vtt?language=eng",
        "title": clean_text(str(media.get("name", ""))) or item.get("title", ""),
        "channel": "ARK Invest",
        "duration": media.get("duration"),
        "height": asset.get("height") if asset else None,
        "metadata_url": metadata_url,
    }
    (work_dir / "selected_source.json").write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    quality = f"{source['height']}p" if source.get("height") else "HLS"
    print(
        f"[ark] Selected official Wistia source: {media_id} / {quality} / "
        f"identity score {identity_score} / binding score {binding_score} / {source['url']}",
        flush=True,
    )
    return source


def download_wistia_video(
    source: dict[str, Any],
    output: Path,
    work_dir: Path,
    max_height: int,
    min_video_seconds: float,
) -> None:
    del work_dir
    output.parent.mkdir(parents=True, exist_ok=True)
    part = output.with_suffix(output.suffix + ".part")
    errors: list[str] = []
    asset_url = clean_text(str(source.get("asset_url", "")))
    if asset_url:
        print(f"[ark] Downloading official Wistia progressive MP4: {asset_url}", flush=True)
        try:
            run([
                "curl",
                "--location",
                "--fail",
                "--show-error",
                "--retry", "8",
                "--retry-all-errors",
                "--retry-delay", "2",
                "--connect-timeout", "20",
                "--speed-time", "90",
                "--speed-limit", "1024",
                "--continue-at", "-",
                "--user-agent", BROWSER_UA,
                "--referer", "https://www.ark-invest.com/",
                "--output", str(part),
                asset_url,
            ])
            if not part.exists() or part.stat().st_size <= 0:
                raise RuntimeError("Wistia progressive download created no file")
            duration = ffprobe_duration(part)
            if duration < min_video_seconds:
                raise RuntimeError(f"Wistia progressive source is too short: {duration:.1f}s")
            part.replace(output)
            return
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            errors.append(f"progressive: {exc}")
            if part.exists():
                part.unlink()
            print("::warning::Wistia progressive download failed; trying official HLS.", flush=True)

    hls_url = clean_text(str(source.get("hls_url", "")))
    if hls_url:
        try:
            run([
                *ytdlp_command(),
                "--no-playlist",
                "--retries", "8",
                "--fragment-retries", "8",
                "--concurrent-fragments", "8",
                "--merge-output-format", "mp4",
                "-f", f"bestvideo[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
                "--force-overwrites",
                "--newline",
                "-o", str(output),
                hls_url,
            ])
            if output.exists() and output.stat().st_size > 0:
                duration = ffprobe_duration(output)
                if duration >= min_video_seconds:
                    return
                raise RuntimeError(f"Wistia HLS source is too short: {duration:.1f}s")
            raise RuntimeError("Wistia HLS download created no file")
        except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
            errors.append(f"hls: {exc}")
            if output.exists():
                output.unlink()
    raise RuntimeError("Official Wistia download failed: " + "; ".join(errors))


def vtt_timestamp_seconds(value: str) -> float:
    normalized = value.strip().replace(",", ".")
    parts = normalized.split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    elif len(parts) == 3:
        hours, minutes, seconds = parts
    else:
        raise ValueError(f"Invalid VTT timestamp: {value}")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_vtt_segments(text: str) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n")):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        timing_index = next((index for index, line in enumerate(lines) if "-->" in line), None)
        if timing_index is None:
            continue
        timing = lines[timing_index].split("-->", 1)
        try:
            start = vtt_timestamp_seconds(timing[0])
            end = vtt_timestamp_seconds(timing[1].split()[0])
        except (ValueError, IndexError):
            continue
        caption = clean_text(
            html.unescape(re.sub(r"<[^>]+>", "", " ".join(lines[timing_index + 1 :])))
        )
        if caption and end > start:
            segments.append({
                "start": round(start, 3),
                "end": round(end, 3),
                "text": caption,
            })
    return segments


def write_wistia_transcript(source: dict[str, Any], transcript_path: Path) -> None:
    caption_url = clean_text(str(source.get("caption_url", "")))
    if not caption_url:
        raise RuntimeError("Wistia source has no English caption URL")
    vtt_text = fetch_text_direct(caption_url, timeout=45)
    segments = parse_vtt_segments(vtt_text)
    if not segments:
        raise RuntimeError("Wistia caption file contained no timed segments")
    (transcript_path.parent / "wistia_captions.vtt").write_text(vtt_text, encoding="utf-8")
    payload = {
        "model": "wistia-captions",
        "language": "en",
        "duration": round(segments[-1]["end"], 3),
        "segments": segments,
    }
    transcript_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[ark] Using official Wistia captions ({len(segments)} timed segments)", flush=True)


def proxy_args(args: argparse.Namespace, source_url: str) -> argparse.Namespace:
    return SimpleNamespace(
        subscription=args.proxy_subscription,
        subscription_url=args.proxy_subscription_url,
        subscription_url_file=args.proxy_subscription_url_file,
        refresh_subscription=args.refresh_proxy_subscription,
        proxy_cache=args.proxy_cache,
        proxy_test_url=args.proxy_test_url,
        google_doh=args.google_doh,
        url=source_url,
    )


def working_proxy(args: argparse.Namespace, source_url: str, work_dir: Path) -> str | None:
    if args.yt_dlp_proxy_mode == "never":
        return None

    proxy_ns = proxy_args(args, source_url)
    proxy = cached_proxy(proxy_ns, work_dir)
    if proxy:
        return proxy

    try:
        subscription = ensure_subscription(proxy_ns)
        return scan_working_proxy(proxy_ns, subscription, work_dir)
    except SystemExit as exc:
        message = str(exc) or exc.__class__.__name__
        print(f"[ark] Proxy setup unavailable: {message}", flush=True)
        if args.yt_dlp_proxy_mode == "always":
            raise
    return None


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
        if is_trump_related(url, title, item.get("description", ""), item.get("slug", ""), use_ai=True):
            print(f"[ark] Skipping sensitive-topic manifest video: {title or url}", flush=True)
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


def resolve_youtube_url(
    item: dict[str, Any],
    work_dir: Path,
    max_search_results: int,
    js_runtime: str = "node",
    pot_provider_url: str = "",
) -> dict[str, Any]:
    query = item["download_query"]
    search_url = f"ytsearch{max_search_results}:{query}"
    command = [
        *ytdlp_command(),
        *ytdlp_js_runtime_args(js_runtime),
        *ytdlp_pot_provider_args(pot_provider_url),
        "--dump-json",
        "--skip-download",
        "--flat-playlist",
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
    candidates = [
        candidate
        for candidate in candidates
        if not is_trump_related(
            candidate.get("title", ""),
            candidate.get("channel", candidate.get("uploader", "")),
            candidate.get("webpage_url", candidate.get("url", "")),
            use_ai=True,
        )
    ]
    if not candidates:
        raise RuntimeError("yt-dlp search returned only sensitive-topic candidates")
    candidates.sort(key=lambda candidate: candidate_score(candidate, item["title"]), reverse=True)
    selected = candidates[0]
    (work_dir / "yt_dlp_selected.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    webpage_url = clean_text(str(selected.get("webpage_url") or selected.get("url") or ""))
    if selected.get("ie_key") == "Youtube" and webpage_url and not webpage_url.startswith("http"):
        webpage_url = f"https://www.youtube.com/watch?v={webpage_url}"
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


def download_video(source_url: str, output: Path, work_dir: Path, args: argparse.Namespace) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    attempts = [
        ("mweb-pot", ["--extractor-args", "youtube:player_client=mweb"]),
        ("default", []),
        ("web-embedded", ["--extractor-args", "youtube:player_client=web_embedded,default"]),
        ("android-web", ["--extractor-args", "youtube:player_client=android,web"]),
        ("tv-web", ["--extractor-args", "youtube:player_client=tv,web"]),
    ]
    errors: list[str] = []
    proxy: str | None = None
    proxy_ready = False
    modes = ["direct"] if args.yt_dlp_proxy_mode == "never" else ["direct", "proxy"]
    if args.yt_dlp_proxy_mode == "always":
        modes = ["proxy"]

    for mode in modes:
        local_proxy_context = None
        local_proxy_url = ""
        if mode == "proxy":
            if not proxy_ready:
                proxy = working_proxy(args, source_url, work_dir)
                proxy_ready = True
            if not proxy:
                errors.append("proxy unavailable")
                continue
            scheme = hls_downloader.proxy_scheme(proxy)
            if scheme not in {"http", "https"}:
                errors.append(f"proxy scheme {scheme or 'unknown'} is not usable with yt-dlp")
                continue
            local_proxy_context = LocalProxyServer(proxy, google_doh=args.google_doh)
            local_proxy_url = local_proxy_context.__enter__()
            print("[ark] Retrying yt-dlp download through cached/scanned proxy", flush=True)

        try:
            for attempt_index, (attempt_name, extra_args) in enumerate(attempts, start=1):
                command = [
                    *ytdlp_command(),
                    *ytdlp_js_runtime_args(args.yt_dlp_js_runtime),
                    *ytdlp_pot_provider_args(args.yt_dlp_pot_provider_url),
                    "--no-playlist",
                    "--retries", "5",
                    "--fragment-retries", "5",
                    "--merge-output-format", "mp4",
                    "--user-agent", BROWSER_UA,
                    "--referer", "https://www.youtube.com/",
                    "-f", "bv*[height<=1080]+ba/b[height<=1080]/best[height<=1080]/best",
                    "-S", "res:1080,ext:mp4:m4a",
                    "--force-overwrites",
                    "--newline",
                    *extra_args,
                    "-o", str(output),
                    source_url,
                ]
                if local_proxy_url:
                    command = [*command[:-1], "--proxy", local_proxy_url, command[-1]]
                command_index = f"{mode}_{attempt_index}"
                (work_dir / f"yt_dlp_download_command_{command_index}.json").write_text(
                    json.dumps(command, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                try:
                    run(command)
                    return
                except subprocess.CalledProcessError as exc:
                    errors.append(f"{mode} {attempt_name} exit {exc.returncode}")
                    if output.exists():
                        output.unlink()
        finally:
            if local_proxy_context:
                local_proxy_context.__exit__(None, None, None)
    raise RuntimeError("yt-dlp download failed after fallback attempts: " + "; ".join(errors))


def resolve_ark_source(
    item: dict[str, Any],
    work_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    print("[ark] Resolving official ARK/Wistia source", flush=True)
    try:
        return resolve_wistia_source(
            item,
            work_dir,
            args.ark_chrome_bin,
            args.wistia_max_height,
        )
    except Exception as exc:  # noqa: BLE001 - preserve YouTube as a secondary source
        print(
            f"::warning::Official ARK/Wistia source resolution failed ({exc}); "
            "falling back to the official ARK Invest YouTube upload.",
            flush=True,
        )
    source = resolve_youtube_url(
        item,
        work_dir,
        args.search_results,
        args.yt_dlp_js_runtime,
        args.yt_dlp_pot_provider_url,
    )
    return {"provider": "youtube", **source}


def download_selected_source(
    source: dict[str, Any],
    output: Path,
    work_dir: Path,
    args: argparse.Namespace,
) -> None:
    if source.get("provider") == "wistia":
        download_wistia_video(
            source,
            output,
            work_dir,
            args.wistia_max_height,
            args.min_video_seconds,
        )
        return
    download_video(clean_text(str(source.get("url", ""))), output, work_dir, args)


def validate_downloaded_source(output: Path, min_video_seconds: float) -> float:
    if not output.exists() or output.stat().st_size <= 0:
        raise RuntimeError(f"Downloaded source file is missing or empty: {output}")
    duration = ffprobe_duration(output)
    if duration < min_video_seconds:
        raise RuntimeError(f"Downloaded video is too short: {duration:.1f}s")
    return duration


def clear_downloaded_source(output: Path) -> None:
    for candidate in (output, output.with_suffix(output.suffix + ".part")):
        if candidate.exists():
            candidate.unlink()


def acquire_ark_source(
    item: dict[str, Any],
    output: Path,
    work_dir: Path,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], float]:
    source = resolve_ark_source(item, work_dir, args)
    try:
        download_selected_source(source, output, work_dir, args)
        return source, validate_downloaded_source(output, args.min_video_seconds)
    except Exception as exc:  # noqa: BLE001 - a complete Wistia failure activates YouTube
        if source.get("provider") != "wistia":
            raise
        clear_downloaded_source(output)
        print(
            f"::warning::Official Wistia source could not produce a valid video ({exc}); "
            "falling back to the official ARK Invest YouTube upload.",
            flush=True,
        )

    youtube_source = resolve_youtube_url(
        item,
        work_dir,
        args.search_results,
        args.yt_dlp_js_runtime,
        args.yt_dlp_pot_provider_url,
    )
    youtube_source = {"provider": "youtube", **youtube_source}
    download_selected_source(youtube_source, output, work_dir, args)
    return youtube_source, validate_downloaded_source(output, args.min_video_seconds)


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
            "media_provider": result.get("media_provider", ""),
            "source_media_url": result.get("source_media_url", ""),
            "wistia_id": result.get("wistia_id", ""),
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

    selected_source, duration = acquire_ark_source(item, video_path, work_dir, args)
    print(f"[ark {index:02d}] Downloaded {duration:.1f}s source: {video_path}", flush=True)

    transcript_ready = False
    if selected_source.get("provider") == "wistia":
        try:
            write_wistia_transcript(selected_source, transcript_path)
            transcript_ready = True
        except Exception as exc:  # noqa: BLE001 - Whisper remains the transcript fallback
            print(
                f"::warning::Official Wistia captions were unavailable ({exc}); "
                "falling back to Whisper transcription.",
                flush=True,
            )
    if not transcript_ready:
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
    title_refinement_status = refine_title_or_keep_planner_title(plan_path)
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    removed = remove_trump_clips_from_plan(plan, use_ai=True)
    if removed:
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"[ark {index:02d}] Removed {len(removed)} sensitive-topic clip(s)", flush=True)
    if not plan.get("clips"):
        raise RuntimeError("ARK video skipped by sensitive topic filter")

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
        "title_refinement_status": title_refinement_status,
        "media_provider": selected_source.get("provider", "youtube"),
        "source_media_url": selected_source.get("url", ""),
        "wistia_id": selected_source.get("media_id", ""),
        "youtube_url": selected_source.get("url", "") if selected_source.get("provider") == "youtube" else "",
        "youtube_title": selected_source.get("title", "") if selected_source.get("provider") == "youtube" else "",
        "youtube_channel": selected_source.get("channel", "") if selected_source.get("provider") == "youtube" else "",
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
    parser.add_argument("--max-videos", type=int, default=1)
    parser.add_argument("--out-root", type=Path, default=ROOT / "rendered-clips")
    parser.add_argument("--work-root", type=Path, default=ROOT / "work" / "ark-invest")
    parser.add_argument("--state", type=Path, default=ROOT / "rendered-clips" / "ark-invest" / "processed_urls.json")
    parser.add_argument("--search-results", type=int, default=5)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--whisper-model", default="base")
    parser.add_argument("--min-video-seconds", type=float, default=30.0)
    parser.add_argument("--max-clip-seconds", type=float, default=110.0)
    parser.add_argument(
        "--ark-chrome-bin",
        default=os.environ.get("ARK_CHROME_BIN", ""),
        help="Optional isolated Chrome binary used only if direct ARK page discovery fails.",
    )
    parser.add_argument(
        "--wistia-max-height",
        type=int,
        default=720,
        help="Maximum progressive/HLS height for the official ARK Wistia source.",
    )
    parser.add_argument("--yt-dlp-proxy-mode", choices=("auto", "never", "always"), default="auto")
    parser.add_argument(
        "--yt-dlp-js-runtime",
        default="node",
        help="JavaScript runtime passed to yt-dlp for YouTube challenge solving.",
    )
    parser.add_argument(
        "--yt-dlp-pot-provider-url",
        default="",
        help="Optional bgutil HTTP PO-token provider base URL passed to yt-dlp.",
    )
    parser.add_argument("--proxy-subscription", type=Path, default=DEFAULT_SUBSCRIPTION)
    parser.add_argument("--proxy-subscription-url", default="")
    parser.add_argument("--proxy-subscription-url-file", type=Path, default=DEFAULT_SUBSCRIPTION_URL_FILE)
    parser.add_argument("--refresh-proxy-subscription", action="store_true")
    parser.add_argument("--proxy-cache", type=Path, default=DEFAULT_PROXY_CACHE)
    parser.add_argument("--proxy-test-url", default=DEFAULT_PROXY_TEST_URL)
    parser.add_argument("--google-doh", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    if args.max_videos < 1:
        raise SystemExit("--max-videos must be at least 1")
    if args.search_results < 1:
        raise SystemExit("--search-results must be at least 1")
    if args.wistia_max_height < 224:
        raise SystemExit("--wistia-max-height must be at least 224")

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

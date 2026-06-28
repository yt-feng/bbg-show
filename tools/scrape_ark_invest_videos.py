#!/usr/bin/env python3
"""Scrape ARK Invest RSS feed for new Cathie Wood video items."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
from download_bloomberg_video import safe_file_part, slug_from_url  # noqa: E402


DEFAULT_FEED_URL = "https://www.ark-invest.com/feed"
DEFAULT_KEYWORDS = (
    "Cathie Wood",
    "In The Know",
    "In the Know",
    "ITK With Cathie",
    "ITK with Cathie",
)
RSS_ATOM_UPDATED = "{http://www.w3.org/2005/Atom}updated"


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value or "")).strip()


def parse_datetime(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"processed": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"processed": []}
    if not isinstance(payload, dict):
        return {"processed": []}
    processed = payload.get("processed")
    if not isinstance(processed, list):
        payload["processed"] = []
    return payload


def processed_keys(state: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for item in state.get("processed", []):
        if not isinstance(item, dict):
            continue
        for key in ("url", "guid"):
            value = clean_text(str(item.get(key, "")))
            if value:
                keys.add(value)
    return keys


def fetch_feed(feed_url: str) -> bytes:
    req = Request(
        feed_url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    with urlopen(req, timeout=30) as response:
        return response.read()


def keyword_match(title: str, description: str, category: str, keywords: list[str]) -> bool:
    haystack = f"{title} {description} {category}".casefold()
    return any(keyword.casefold() in haystack for keyword in keywords if keyword.strip())


def parse_video_items(
    xml_bytes: bytes,
    *,
    keywords: list[str],
    now: datetime,
    lookback_days: int,
    processed: set[str],
    ignore_state: bool,
    max_videos: int,
) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    channel = root.find("channel")
    if channel is None:
        raise SystemExit("RSS feed did not contain a channel")

    cutoff = now - timedelta(days=lookback_days)
    videos: list[dict[str, Any]] = []
    for item in channel.findall("item"):
        item_type = clean_text(item.findtext("type"))
        if item_type.casefold() != "videos":
            continue

        title = clean_text(item.findtext("title"))
        description = clean_text(item.findtext("description"))
        category = clean_text(item.findtext("category"))
        url = clean_text(item.findtext("link"))
        guid = clean_text(item.findtext("guid")) or url
        pub_date = parse_datetime(item.findtext("pubDate") or "")
        updated = parse_datetime(item.findtext(RSS_ATOM_UPDATED) or "")
        item_date = pub_date or updated
        if item_date is None:
            continue
        item_date = item_date.astimezone(now.tzinfo)

        if item_date < cutoff:
            continue
        if not keyword_match(title, description, category, keywords):
            continue
        if not ignore_state and (url in processed or guid in processed):
            continue

        slug = safe_file_part(slug_from_url(url) or title) or f"ark-video-{len(videos) + 1}"
        videos.append({
            "url": url,
            "guid": guid,
            "title": title,
            "source_title": title,
            "description": description,
            "category": category,
            "type": item_type,
            "pub_date": item_date.isoformat(),
            "updated": updated.astimezone(now.tzinfo).isoformat() if updated else "",
            "slug": slug,
            "speaker": "Cathie Wood",
            "source": "ark-invest",
            "download_query": f"{title} ARK Invest Cathie Wood",
        })

    videos.sort(key=lambda row: row.get("pub_date", ""), reverse=True)
    return videos[:max_videos]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed-url", default=DEFAULT_FEED_URL)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("rendered-clips/ark-invest/processed_urls.json"))
    parser.add_argument("--max-videos", type=int, default=1)
    parser.add_argument("--lookback-days", type=int, default=45)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument("--ignore-state", action="store_true")
    parser.add_argument(
        "--keyword",
        action="append",
        default=[],
        help="Keyword that must appear in title/category/description. Can be repeated.",
    )
    args = parser.parse_args()

    if args.max_videos < 1:
        raise SystemExit("--max-videos must be at least 1")
    if args.lookback_days < 1:
        raise SystemExit("--lookback-days must be at least 1")

    timezone = ZoneInfo(args.timezone)
    now = datetime.now(timezone)
    state = load_state(args.state)
    keywords = args.keyword or list(DEFAULT_KEYWORDS)
    xml_bytes = fetch_feed(args.feed_url)
    videos = parse_video_items(
        xml_bytes,
        keywords=keywords,
        now=now,
        lookback_days=args.lookback_days,
        processed=processed_keys(state),
        ignore_state=args.ignore_state,
        max_videos=args.max_videos,
    )
    payload = {
        "source": "ark-invest",
        "feed_url": args.feed_url,
        "generated_at": now.isoformat(),
        "lookback_days": args.lookback_days,
        "keywords": keywords,
        "state": str(args.state),
        "count": len(videos),
        "videos": videos,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

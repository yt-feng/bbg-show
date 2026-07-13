#!/usr/bin/env python3
"""Stable source identities and the cross-run Top Videos success ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit


LEDGER_VERSION = 1
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}


def youtube_video_id(url: str) -> str:
    """Return a YouTube video ID for the URL forms used by the backup feed."""
    parts = urlsplit(str(url).strip())
    host = (parts.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return ""
    if host.endswith("youtu.be"):
        return parts.path.strip("/").split("/", 1)[0]
    if parts.path.rstrip("/") == "/watch":
        return str(parse_qs(parts.query).get("v", [""])[0]).strip()
    pieces = [piece for piece in parts.path.split("/") if piece]
    if len(pieces) >= 2 and pieces[0].lower() in {"shorts", "embed", "live"}:
        return pieces[1].strip()
    return ""


def canonical_source_url(url: str, youtube_id: str = "") -> str:
    """Normalize a source URL without discarding a YouTube watch identity."""
    raw = str(url).strip()
    video_id = str(youtube_id).strip() or youtube_video_id(raw)
    if video_id:
        return f"https://www.youtube.com/watch?v={video_id}"
    parts = urlsplit(raw)
    if not parts.netloc:
        return raw
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host in {"bloomberg.com", "www.bloomberg.com"}:
        scheme = "https"
        host = "www.bloomberg.com"
    if parts.port:
        host = f"{host}:{parts.port}"
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, "", ""))


def source_key(url: str, youtube_id: str = "") -> str:
    """Build the stable identity used for selection and ledger entries."""
    video_id = str(youtube_id).strip() or youtube_video_id(url)
    if video_id:
        return f"youtube:{video_id}"
    normalized = canonical_source_url(url)
    return f"url:{normalized}" if normalized else ""


def item_source_key(item: dict[str, Any]) -> str:
    return source_key(str(item.get("url", "")), str(item.get("youtube_id", "")))


def _ledger_records(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read Top Videos source ledger {path}: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"version", "sources"}:
        raise ValueError(f"Top Videos source ledger has an invalid root: {path}")
    if type(payload["version"]) is not int or payload["version"] != LEDGER_VERSION:
        raise ValueError(f"Top Videos source ledger version must be integer {LEDGER_VERSION}: {path}")
    if not isinstance(payload["sources"], list):
        raise ValueError(f"Top Videos source ledger sources must be an array: {path}")

    records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(payload["sources"]):
        if not isinstance(raw, dict):
            raise ValueError(f"Top Videos source ledger sources[{index}] must be an object: {path}")
        key = str(raw.get("source_key", "")).strip() or item_source_key(raw)
        if not key:
            raise ValueError(f"Top Videos source ledger sources[{index}] has no stable source key: {path}")
        if key in records:
            raise ValueError(f"Top Videos source ledger contains duplicate key {key!r}: {path}")
        records[key] = dict(raw, source_key=key)
    return records


def successful_source_keys_from_summary(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict) or not isinstance(payload.get("videos"), list):
        return set()
    return {
        key
        for raw in payload["videos"]
        if isinstance(raw, dict) and raw.get("status") == "success"
        if (key := item_source_key(raw))
    }


def load_processed_source_keys(ledger_path: Path, history_root: Path) -> set[str]:
    """Load durable identities plus successful items retained in dated summaries."""
    keys = set(_ledger_records(ledger_path))
    if history_root.is_dir():
        for summary_path in sorted(history_root.glob("*/summary.json")):
            keys.update(successful_source_keys_from_summary(summary_path))
    return keys


def update_processed_sources(ledger_path: Path, summary_path: Path) -> int:
    """Record only successful summary items; repeated calls produce identical JSON."""
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read Top Videos summary {summary_path}: {exc}") from exc
    if not isinstance(summary, dict) or not isinstance(summary.get("videos"), list):
        raise ValueError(f"Top Videos summary has no videos list: {summary_path}")

    run_date = str(summary.get("run_date", "")).strip()
    records = _ledger_records(ledger_path)
    recorded = 0
    for raw in summary["videos"]:
        if not isinstance(raw, dict) or raw.get("status") != "success":
            continue
        key = item_source_key(raw)
        if not key:
            continue
        existing = records.get(key, {})
        first_processed_on = str(existing.get("first_processed_on", "")).strip() or run_date
        last_processed_on = str(existing.get("last_processed_on", "")).strip() or run_date
        if run_date:
            first_processed_on = min(first_processed_on, run_date) if first_processed_on else run_date
            last_processed_on = max(last_processed_on, run_date) if last_processed_on else run_date
        record = {
            "source_key": key,
            "url": canonical_source_url(
                str(raw.get("url", existing.get("url", ""))),
                str(raw.get("youtube_id", existing.get("youtube_id", ""))),
            ),
            "source": str(raw.get("source", existing.get("source", "bloomberg"))),
            "youtube_id": str(raw.get("youtube_id", existing.get("youtube_id", ""))),
            "title": str(raw.get("source_title", raw.get("title", existing.get("title", "")))),
            "first_processed_on": first_processed_on,
            "last_processed_on": last_processed_on,
        }
        records[key] = record
        recorded += 1

    payload = {
        "version": LEDGER_VERSION,
        "sources": [records[key] for key in sorted(records)],
    }
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    if not ledger_path.is_file() or ledger_path.read_text(encoding="utf-8") != serialized:
        ledger_path.write_text(serialized, encoding="utf-8")
    return recorded

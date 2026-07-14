#!/usr/bin/env python3
"""Stable source identities and the cross-run Top Videos success ledger."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from content_fingerprint import (
    fingerprints_from_plan,
    same_clip,
    same_full_source,
    validate_fingerprint,
)

LEDGER_VERSION = 2
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
_LEDGER_ROOT_KEYS = {"version", "sources", "clips", "history_backfilled"}


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


def _empty_ledger() -> dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "sources": [],
        "clips": [],
        "history_backfilled": False,
    }


def _validated_fingerprint(value: Any, context: str) -> Any:
    if value in (None, ""):
        return None
    try:
        valid = validate_fingerprint(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context} has an invalid fingerprint: {exc}") from exc
    if valid is False:
        raise ValueError(f"{context} has an invalid fingerprint")
    return valid


def _validate_sources(raw_sources: Any, path: Path) -> list[dict[str, Any]]:
    if not isinstance(raw_sources, list):
        raise ValueError(f"Top Videos source ledger sources must be an array: {path}")
    records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_sources):
        if not isinstance(raw, dict):
            raise ValueError(f"Top Videos source ledger sources[{index}] must be an object: {path}")
        key = str(raw.get("source_key", "")).strip() or item_source_key(raw)
        if not key:
            raise ValueError(f"Top Videos source ledger sources[{index}] has no stable source key: {path}")
        if key in records:
            raise ValueError(f"Top Videos source ledger contains duplicate key {key!r}: {path}")
        record = dict(raw, source_key=key)
        if "source_fingerprint" in record:
            fingerprint = _validated_fingerprint(
                record.get("source_fingerprint"),
                f"Top Videos source ledger sources[{index}]",
            )
            if fingerprint is None:
                record.pop("source_fingerprint", None)
            else:
                record["source_fingerprint"] = fingerprint
        records[key] = record
    return [records[key] for key in sorted(records)]


def _validate_clips(raw_clips: Any, path: Path) -> list[dict[str, Any]]:
    if not isinstance(raw_clips, list):
        raise ValueError(f"Top Videos source ledger clips must be an array: {path}")
    clips: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_clips):
        if not isinstance(raw, dict):
            raise ValueError(f"Top Videos source ledger clips[{index}] must be an object: {path}")
        fingerprint = _validated_fingerprint(
            raw.get("clip_fingerprint"),
            f"Top Videos source ledger clips[{index}]",
        )
        if fingerprint is None:
            raise ValueError(
                f"Top Videos source ledger clips[{index}] has no clip fingerprint: {path}"
            )
        clips.append(dict(raw, clip_fingerprint=fingerprint))
    return clips


def load_ledger(path: Path) -> dict[str, Any]:
    """Load either ledger generation and return a validated v2 payload."""
    path = Path(path)
    if not path.is_file():
        return _empty_ledger()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read Top Videos source ledger {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Top Videos source ledger has an invalid root: {path}")

    version = payload.get("version")
    if type(version) is not int or version not in {1, LEDGER_VERSION}:
        raise ValueError(
            f"Top Videos source ledger version must be integer 1 or {LEDGER_VERSION}: {path}"
        )
    expected_keys = {"version", "sources"} if version == 1 else _LEDGER_ROOT_KEYS
    if set(payload) != expected_keys:
        raise ValueError(f"Top Videos source ledger has an invalid root: {path}")

    ledger = _empty_ledger()
    ledger["sources"] = _validate_sources(payload.get("sources"), path)
    if version == LEDGER_VERSION:
        ledger["clips"] = _validate_clips(payload.get("clips"), path)
        if type(payload.get("history_backfilled")) is not bool:
            raise ValueError(
                f"Top Videos source ledger history_backfilled must be boolean: {path}"
            )
        ledger["history_backfilled"] = payload["history_backfilled"]
    return ledger


def _ledger_records(path: Path) -> dict[str, dict[str, Any]]:
    """Compatibility helper for callers that only need URL identities."""
    return {record["source_key"]: record for record in load_ledger(path)["sources"]}


def _fingerprint_sort_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _clip_identity(record: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(record.get("source_key", "")),
        str(record.get("processed_on", "")),
        str(record.get("clip_index", "")),
        _fingerprint_sort_key(record.get("clip_fingerprint")),
    )


def _sorted_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    sources = {
        str(record["source_key"]): record
        for record in ledger.get("sources", [])
    }
    unique_clips: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for record in ledger.get("clips", []):
        unique_clips.setdefault(_clip_identity(record), record)
    return {
        "version": LEDGER_VERSION,
        "sources": [sources[key] for key in sorted(sources)],
        "clips": [unique_clips[key] for key in sorted(unique_clips)],
        "history_backfilled": bool(ledger.get("history_backfilled", False)),
    }


def _write_ledger_atomic(path: Path, ledger: dict[str, Any]) -> None:
    path = Path(path)
    payload = _sorted_ledger(ledger)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if path.is_file() and path.read_text(encoding="utf-8") == serialized:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _duplicate_description(record: dict[str, Any]) -> str:
    parts = [
        str(record.get("processed_on", record.get("first_processed_on", ""))).strip(),
        str(record.get("title", record.get("source_title", ""))).strip(),
        str(record.get("url", record.get("source_key", ""))).strip(),
    ]
    return " · ".join(part for part in parts if part)


def find_duplicate_content(
    ledger_or_path: dict[str, Any] | Path | str,
    source_fingerprint: Any = None,
    source_duration: float = 0,
    clip_fingerprints: list[Any] | None = None,
) -> dict[str, Any] | None:
    """Return a readable historical match for an identical source or clip."""
    if isinstance(ledger_or_path, dict):
        ledger = ledger_or_path
    else:
        ledger = load_ledger(Path(ledger_or_path))

    incoming_source = _validated_fingerprint(
        source_fingerprint,
        "Top Videos duplicate source candidate",
    )
    if incoming_source is not None:
        for record in ledger.get("sources", []):
            existing_source = record.get("source_fingerprint")
            if existing_source in (None, ""):
                continue
            existing_source = _validated_fingerprint(
                existing_source,
                f"Top Videos duplicate source history {record.get('source_key', '')!r}",
            )
            if same_full_source(
                incoming_source,
                existing_source,
                float(source_duration or 0),
                float(record.get("source_duration", 0) or 0),
            ):
                match = dict(record)
                match["duplicate_kind"] = "full_source"
                match["duplicate_of"] = _duplicate_description(record)
                return match

    candidates = clip_fingerprints or []
    for raw_candidate in candidates:
        if isinstance(raw_candidate, dict) and "clip_fingerprint" in raw_candidate:
            raw_candidate = raw_candidate["clip_fingerprint"]
        candidate = _validated_fingerprint(
            raw_candidate,
            "Top Videos duplicate clip candidate",
        )
        if candidate is None:
            continue
        for record in ledger.get("clips", []):
            existing_clip = _validated_fingerprint(
                record.get("clip_fingerprint"),
                f"Top Videos duplicate clip history {record.get('source_key', '')!r}",
            )
            if existing_clip is not None and same_clip(candidate, existing_clip):
                match = dict(record)
                match["duplicate_kind"] = "clip"
                match["duplicate_of"] = _duplicate_description(record)
                return match
    return None


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
    ledger = load_ledger(ledger_path)
    records = {record["source_key"]: record for record in ledger["sources"]}
    clips = list(ledger["clips"])
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
        record = dict(existing)
        record.update({
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
        })
        incoming_source_fingerprint = _validated_fingerprint(
            raw.get("source_fingerprint"),
            f"Top Videos summary success {key!r}",
        )
        if "source_fingerprint" not in record and incoming_source_fingerprint is not None:
            record["source_fingerprint"] = incoming_source_fingerprint
        incoming_source_duration = raw.get("source_duration", raw.get("duration", 0))
        if "source_duration" not in record and isinstance(incoming_source_duration, (int, float)):
            if float(incoming_source_duration) > 0:
                record["source_duration"] = round(float(incoming_source_duration), 3)
        records[key] = record

        raw_clip_fingerprints = raw.get("clip_fingerprints", [])
        if raw_clip_fingerprints is None:
            raw_clip_fingerprints = []
        if not isinstance(raw_clip_fingerprints, list):
            raise ValueError(f"Top Videos summary success {key!r} clip_fingerprints must be an array")
        for clip_index, raw_clip in enumerate(raw_clip_fingerprints, start=1):
            details = dict(raw_clip) if isinstance(raw_clip, dict) else {}
            fingerprint_value = details.pop("clip_fingerprint", raw_clip)
            fingerprint = _validated_fingerprint(
                fingerprint_value,
                f"Top Videos summary success {key!r} clip_fingerprints[{clip_index - 1}]",
            )
            if fingerprint is None:
                continue
            clip_record = {
                "clip_fingerprint": fingerprint,
                "source_key": key,
                "url": record["url"],
                "source_title": record["title"],
                "processed_on": run_date,
                "clip_index": int(details.pop("clip_index", clip_index)),
            }
            for field in ("title", "start", "end", "duration"):
                if field in details:
                    clip_record[field] = details[field]
            clips.append(clip_record)
        recorded += 1

    ledger["sources"] = [records[key] for key in sorted(records)]
    ledger["clips"] = clips
    _write_ledger_atomic(ledger_path, ledger)
    return recorded


def _source_duration_from_plan(plan: dict[str, Any]) -> float:
    raw_range = plan.get("source_segment_range")
    if (
        isinstance(raw_range, list)
        and len(raw_range) == 2
        and all(isinstance(value, (int, float)) for value in raw_range)
    ):
        duration = float(raw_range[1]) - float(raw_range[0])
        if duration > 0:
            return round(duration, 3)
    duration = plan.get("source_duration", plan.get("duration", 0))
    if isinstance(duration, (int, float)) and float(duration) > 0:
        return round(float(duration), 3)
    return 0.0


def _plan_fingerprint_values(plan: dict[str, Any]) -> tuple[Any, list[Any]]:
    """Accept the public plan helper's mapping or two-item tuple result."""
    result = fingerprints_from_plan(plan)
    if isinstance(result, dict):
        source_fingerprint = result.get("source_fingerprint")
        clip_fingerprints = result.get("clip_fingerprints", [])
    elif isinstance(result, tuple) and len(result) == 2:
        source_fingerprint, clip_fingerprints = result
    else:
        source_fingerprint, clip_fingerprints = None, result
    if clip_fingerprints is None:
        clip_fingerprints = []
    if not isinstance(clip_fingerprints, list):
        clip_fingerprints = list(clip_fingerprints)
    return source_fingerprint, clip_fingerprints


def _history_date(path: str) -> str:
    parts = Path(path).parts
    try:
        index = parts.index("top-videos")
    except ValueError:
        return ""
    return parts[index + 1] if len(parts) > index + 1 else ""


def _append_plan_to_ledger(
    ledger: dict[str, Any],
    plan: dict[str, Any],
    *,
    processed_on: str,
) -> bool:
    raw_url = str(plan.get("source_url", "")).strip()
    key = source_key(raw_url)
    if not key:
        return False
    url = canonical_source_url(raw_url)
    records = {record["source_key"]: record for record in ledger["sources"]}
    existing = records.get(key, {})
    first_processed_on = str(existing.get("first_processed_on", "")).strip() or processed_on
    last_processed_on = str(existing.get("last_processed_on", "")).strip() or processed_on
    if processed_on:
        first_processed_on = min(first_processed_on, processed_on) if first_processed_on else processed_on
        last_processed_on = max(last_processed_on, processed_on) if last_processed_on else processed_on

    record = dict(existing)
    record.update(
        {
            "source_key": key,
            "url": url,
            "source": str(existing.get("source", "youtube-backup" if youtube_video_id(url) else "bloomberg")),
            "youtube_id": str(existing.get("youtube_id", youtube_video_id(url))),
            "title": str(existing.get("title", plan.get("source_title", ""))),
            "first_processed_on": first_processed_on,
            "last_processed_on": last_processed_on,
        }
    )
    source_fingerprint, clip_fingerprints = _plan_fingerprint_values(plan)
    validated_source = _validated_fingerprint(
        source_fingerprint,
        f"Top Videos historical plan {processed_on or url!r}",
    )
    if "source_fingerprint" not in record and validated_source is not None:
        record["source_fingerprint"] = validated_source
    source_duration = _source_duration_from_plan(plan)
    if "source_duration" not in record and source_duration:
        record["source_duration"] = source_duration
    records[key] = record
    ledger["sources"] = [records[item_key] for item_key in sorted(records)]

    plan_clips = plan.get("clips", [])
    if not isinstance(plan_clips, list):
        plan_clips = []
    for index, raw_fingerprint in enumerate(clip_fingerprints, start=1):
        details = dict(raw_fingerprint) if isinstance(raw_fingerprint, dict) else {}
        fingerprint_value = details.pop(
            "clip_fingerprint",
            details.pop("fingerprint", raw_fingerprint),
        )
        fingerprint = _validated_fingerprint(
            fingerprint_value,
            f"Top Videos historical plan {processed_on or url!r} clip {index}",
        )
        if fingerprint is None:
            continue
        raw_clip = plan_clips[index - 1] if index <= len(plan_clips) else {}
        if not isinstance(raw_clip, dict):
            raw_clip = {}
        clip_record: dict[str, Any] = {
            "clip_fingerprint": fingerprint,
            "source_key": key,
            "url": url,
            "source_title": record["title"],
            "processed_on": processed_on,
            "clip_index": int(details.get("clip_index", index)),
        }
        for field in ("title", "start", "end", "duration"):
            value = details.get(field, raw_clip.get(field))
            if value not in (None, ""):
                clip_record[field] = value
        ledger["clips"].append(clip_record)
    return True


def backfill_processed_sources_from_git_history(
    ledger_path: Path,
    repo_root: Path,
) -> int:
    """Import every unique historical Top Videos plan blob, including deleted plans."""
    ledger = load_ledger(ledger_path)
    if ledger["history_backfilled"]:
        return 0
    repo_root = Path(repo_root)
    try:
        # Avoid ``rev-list --objects`` here.  This repository has multi-gigabyte
        # video packs, and enumerating every reachable object can take minutes.
        # The path touches only a few dozen commits, so walking their trees is
        # both complete (including deleted files) and much faster.
        commits = subprocess.run(
            [
                "git",
                "log",
                "--all",
                "--format=%H",
                "--",
                "rendered-clips/top-videos",
            ],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        paths_by_blob: dict[str, str] = {}
        for commit in dict.fromkeys(commits):
            tree = subprocess.run(
                [
                    "git",
                    "ls-tree",
                    "-r",
                    "-z",
                    commit,
                    "--",
                    "rendered-clips/top-videos",
                ],
                cwd=repo_root,
                check=True,
                capture_output=True,
            ).stdout
            for raw_entry in tree.split(b"\0"):
                if not raw_entry or b"\t" not in raw_entry:
                    continue
                metadata, raw_path = raw_entry.split(b"\t", 1)
                fields = metadata.split()
                if len(fields) != 3 or fields[1] != b"blob":
                    continue
                path = raw_path.decode("utf-8", errors="surrogateescape")
                if not path.endswith("/highlight_plan.json"):
                    continue
                blob = fields[2].decode("ascii")
                paths_by_blob[blob] = min(path, paths_by_blob.get(blob, path))
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(f"Could not enumerate historical Top Videos plans: {exc}") from exc

    imported = 0
    for blob, path in sorted(paths_by_blob.items(), key=lambda item: (item[1], item[0])):
        try:
            raw_plan = subprocess.run(
                ["git", "cat-file", "blob", blob],
                cwd=repo_root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            plan = json.loads(raw_plan)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
            continue
        if not isinstance(plan, dict):
            continue
        if _append_plan_to_ledger(ledger, plan, processed_on=_history_date(path)):
            imported += 1

    ledger["history_backfilled"] = True
    _write_ledger_atomic(ledger_path, ledger)
    return imported

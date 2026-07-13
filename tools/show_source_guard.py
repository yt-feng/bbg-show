#!/usr/bin/env python3
"""Reject stale or previously published Bloomberg show sources.

The guard runs twice in the daily workflow.  The first pass uses the Bloomberg
asset and manifest metadata, so an obviously stale source can be skipped before
transcription.  The second pass adds a transcript signature, which catches the
same programme even when Bloomberg republishes it under a new URL or asset ID.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SCHEMA_VERSION = 1
IDENTITY_VERSION = 1
DEFAULT_LEDGER = Path("rendered-clips/show_sources.json")
TRACKING_QUERY_PREFIXES = ("utm_",)
TRACKING_QUERY_KEYS = frozenset({"cmpid", "srnd", "leadsource", "sref"})
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.IGNORECASE)
MAX_TRANSCRIPT_SIMHASH_DISTANCE = 20


class SourceGuardError(ValueError):
    """Raised when source identity input or the durable ledger is malformed."""


def empty_ledger() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "sources": []}


def canonical_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    parts = urlsplit(value)
    if not parts.scheme or not parts.netloc:
        return value
    query = [
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not any(key.lower().startswith(prefix) for prefix in TRACKING_QUERY_PREFIXES)
    ]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(sorted(query)), ""))


def parse_show_date(value: Any, *, context: str) -> str:
    if not isinstance(value, str):
        raise SourceGuardError(f"{context} must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SourceGuardError(f"{context} must be a valid YYYY-MM-DD date: {value!r}") from exc
    if parsed.isoformat() != value:
        raise SourceGuardError(f"{context} must use canonical YYYY-MM-DD format: {value!r}")
    return value


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise SourceGuardError("processed_at must include timezone information")
    current = current.astimezone(timezone.utc)
    return current.isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_timestamp(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SourceGuardError(f"{context} must be a UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SourceGuardError(f"{context} is not a valid timestamp: {value!r}") from exc
    if utc_timestamp(parsed) != value:
        raise SourceGuardError(f"{context} must use canonical second-precision UTC format: {value!r}")
    return value


def normalize_words(text: str) -> list[str]:
    return [match.group(0).lower() for match in WORD_RE.finditer(text)]


def transcript_text(payload: Any) -> str:
    if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
        raise SourceGuardError("transcript must contain a segments array")
    values: list[str] = []
    for index, segment in enumerate(payload["segments"]):
        if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
            raise SourceGuardError(f"transcript segments[{index}].text must be a string")
        values.append(segment["text"])
    return " ".join(values)


def simhash(words: list[str], *, bits: int = 128, shingle_size: int = 4) -> str:
    if len(words) < shingle_size:
        return ""
    weights = [0] * bits
    for index in range(len(words) - shingle_size + 1):
        shingle = " ".join(words[index : index + shingle_size]).encode("utf-8")
        value = int.from_bytes(hashlib.blake2b(shingle, digest_size=bits // 8).digest(), "big")
        for bit in range(bits):
            weights[bit] += 1 if value & (1 << bit) else -1
    result = 0
    for bit, weight in enumerate(weights):
        if weight >= 0:
            result |= 1 << bit
    return f"{result:0{bits // 4}x}"


def hamming_distance(left: str, right: str) -> int:
    if not left or len(left) != len(right):
        return max(len(left), len(right)) * 4
    return bin(int(left, 16) ^ int(right, 16)).count("1")


def walk_key_values(value: Any, wanted: set[str]) -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in wanted:
                yield key, item
            yield from walk_key_values(item, wanted)
    elif isinstance(value, list):
        for item in value:
            yield from walk_key_values(item, wanted)


def first_manifest_text(manifest: dict[str, Any], *keys: str) -> str:
    for key in keys:
        for _found_key, value in walk_key_values(manifest, {key.casefold()}):
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def manifest_asset_id(manifest: dict[str, Any]) -> str:
    for _key, value in walk_key_values(manifest, {"assetid", "bmmpid", "bmmrid", "id"}):
        if isinstance(value, str):
            match = UUID_RE.fullmatch(value.strip())
            if match:
                return match.group(0).lower()
    return ""


def dates_in_text(text: str) -> set[str]:
    found: set[str] = set()
    for match in re.finditer(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", text):
        try:
            found.add(date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat())
        except ValueError:
            pass
    for match in re.finditer(r"(?<!\d)(\d{1,2})[-/](\d{1,2})[-/](20\d{2})(?!\d)", text):
        try:
            found.add(date(int(match.group(3)), int(match.group(1)), int(match.group(2))).isoformat())
        except ValueError:
            pass
    return found


def validate_manifest(manifest: dict[str, Any], show_date: str, show_type: str) -> str:
    title = first_manifest_text(manifest, "webTitle", "title", "headline")
    source_url = first_manifest_text(manifest, "sourceURL", "sourceUrl")
    explicit_dates = dates_in_text(" ".join(item for item in (title, source_url) if item))
    if explicit_dates and show_date not in explicit_dates:
        return f"Bloomberg manifest is dated {', '.join(sorted(explicit_dates))}, expected {show_date}"

    show_name = first_manifest_text(manifest, "showName")
    normalized_name = f"{show_name} {title}".casefold()
    if show_type == "weekend" and "china show" in normalized_name and "weekend" not in normalized_name:
        return f"Bloomberg manifest is for {show_name or title}, expected Bloomberg Weekend"
    if show_type == "china" and "weekend" in normalized_name and "china show" not in normalized_name:
        return f"Bloomberg manifest is for {show_name or title}, expected Bloomberg China Show"
    return ""


def strict_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SourceGuardError(f"Cannot read valid JSON from {path}: {exc}") from exc


def optional_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = strict_json(path)
    except SourceGuardError as exc:
        print(f"::warning::{exc}; continuing with asset and transcript guards")
        return {}
    if not isinstance(payload, dict):
        print(f"::warning::Bloomberg manifest is not a JSON object: {path}; continuing without it")
        return {}
    return payload


def validate_identity(payload: Any, *, require_transcript: bool = False) -> dict[str, Any]:
    required = {
        "identity_version",
        "show_date",
        "show_type",
        "canonical_url",
        "asset_id",
        "manifest_title",
        "manifest_show_name",
        "duration",
        "word_count",
        "transcript_sha256",
        "transcript_simhash",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise SourceGuardError("source identity has invalid keys")
    if payload["identity_version"] != IDENTITY_VERSION:
        raise SourceGuardError(f"identity_version must be {IDENTITY_VERSION}")
    parse_show_date(payload["show_date"], context="identity.show_date")
    if payload["show_type"] not in {"china", "weekend"}:
        raise SourceGuardError("identity.show_type must be china or weekend")
    for key in ("canonical_url", "asset_id", "manifest_title", "manifest_show_name", "transcript_sha256", "transcript_simhash"):
        if not isinstance(payload[key], str):
            raise SourceGuardError(f"identity.{key} must be a string")
    if type(payload["word_count"]) is not int or payload["word_count"] < 0:
        raise SourceGuardError("identity.word_count must be a non-negative integer")
    if not isinstance(payload["duration"], (int, float)) or payload["duration"] < 0:
        raise SourceGuardError("identity.duration must be non-negative")
    if require_transcript and (payload["word_count"] < 1 or not payload["transcript_sha256"]):
        raise SourceGuardError("recorded source identity must include a transcript signature")
    return payload


def validate_ledger(payload: Any, *, source: Path | str = "ledger") -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "sources"}:
        raise SourceGuardError(f"{source}: invalid ledger root")
    if payload["schema_version"] != SCHEMA_VERSION or type(payload["schema_version"]) is not int:
        raise SourceGuardError(f"{source}: schema_version must be integer {SCHEMA_VERSION}")
    if not isinstance(payload["sources"], list):
        raise SourceGuardError(f"{source}: sources must be an array")
    seen_dates: set[str] = set()
    required = {
        "show_date",
        "show_type",
        "canonical_url",
        "asset_id",
        "duration",
        "word_count",
        "transcript_sha256",
        "transcript_simhash",
        "processed_at",
    }
    for index, item in enumerate(payload["sources"]):
        context = f"{source}: sources[{index}]"
        if not isinstance(item, dict) or set(item) != required:
            raise SourceGuardError(f"{context} has invalid keys")
        show_date = parse_show_date(item["show_date"], context=f"{context}.show_date")
        if show_date in seen_dates:
            raise SourceGuardError(f"{source}: duplicate show_date {show_date}")
        seen_dates.add(show_date)
        if item["show_type"] not in {"china", "weekend"}:
            raise SourceGuardError(f"{context}.show_type must be china or weekend")
        for key in ("canonical_url", "asset_id", "transcript_sha256", "transcript_simhash"):
            if not isinstance(item[key], str):
                raise SourceGuardError(f"{context}.{key} must be a string")
        if type(item["word_count"]) is not int or item["word_count"] < 1:
            raise SourceGuardError(f"{context}.word_count must be a positive integer")
        if not isinstance(item["duration"], (int, float)) or item["duration"] < 0:
            raise SourceGuardError(f"{context}.duration must be non-negative")
        parse_timestamp(item["processed_at"], context=f"{context}.processed_at")
    return payload


def load_ledger(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_ledger()
    return validate_ledger(strict_json(path), source=path)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def build_identity(
    metadata: dict[str, Any],
    plan: dict[str, Any],
    manifest: dict[str, Any],
    transcript: dict[str, Any] | None,
) -> dict[str, Any]:
    show_date = parse_show_date(metadata.get("SHOW_DATE"), context="show metadata SHOW_DATE")
    show_type = metadata.get("SHOW_TYPE")
    if show_type not in {"china", "weekend"}:
        raise SourceGuardError("show metadata SHOW_TYPE must be china or weekend")
    requested_url = str(plan.get("url") or metadata.get("SHOW_URL") or "")
    asset_id = str(plan.get("asset_id") or manifest_asset_id(manifest) or "").strip().lower()
    if asset_id and not UUID_RE.fullmatch(asset_id):
        raise SourceGuardError(f"download plan contains an invalid asset_id: {asset_id!r}")

    words: list[str] = []
    duration = 0.0
    if transcript is not None:
        words = normalize_words(transcript_text(transcript))
        raw_duration = transcript.get("duration", 0)
        if isinstance(raw_duration, (int, float)):
            duration = round(float(raw_duration), 2)
    normalized = " ".join(words)
    return {
        "identity_version": IDENTITY_VERSION,
        "show_date": show_date,
        "show_type": show_type,
        "canonical_url": canonical_url(requested_url),
        "asset_id": asset_id,
        "manifest_title": first_manifest_text(manifest, "webTitle", "title", "headline"),
        "manifest_show_name": first_manifest_text(manifest, "showName"),
        "duration": duration,
        "word_count": len(words),
        "transcript_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest() if words else "",
        "transcript_simhash": simhash(words),
    }


def transcript_is_same(current: dict[str, Any], previous: dict[str, Any]) -> bool:
    if not current["transcript_sha256"] or not previous["transcript_sha256"]:
        return False
    if current["transcript_sha256"] == previous["transcript_sha256"]:
        return True
    if min(current["word_count"], previous["word_count"]) < 200:
        return False
    word_ratio = min(current["word_count"], previous["word_count"]) / max(
        current["word_count"], previous["word_count"]
    )
    durations = (float(current["duration"]), float(previous["duration"]))
    duration_close = max(durations) == 0 or abs(durations[0] - durations[1]) <= max(15.0, max(durations) * 0.03)
    return (
        word_ratio >= 0.85
        and duration_close
        and hamming_distance(current["transcript_simhash"], previous["transcript_simhash"])
        <= MAX_TRANSCRIPT_SIMHASH_DISTANCE
    )


def ledger_duplicate(identity: dict[str, Any], ledger: dict[str, Any]) -> tuple[str, str]:
    for previous in ledger["sources"]:
        if previous["show_date"] == identity["show_date"]:
            continue
        if identity["asset_id"] and identity["asset_id"] == previous["asset_id"]:
            return previous["show_date"], f"Bloomberg asset {identity['asset_id']} was already published"
        if identity["canonical_url"] and identity["canonical_url"] == previous["canonical_url"]:
            return previous["show_date"], "Bloomberg page URL was already published"
        if transcript_is_same(identity, previous):
            return previous["show_date"], "show transcript matches a previously published programme"
    return "", ""


def bootstrap_url_duplicate(identity: dict[str, Any], rendered_root: Path) -> tuple[str, str]:
    if not identity["canonical_url"] or not rendered_root.exists():
        return "", ""
    for path in sorted(rendered_root.glob("*/show.json")):
        if not DATE_DIR_RE.fullmatch(path.parent.name) or path.parent.name == identity["show_date"]:
            continue
        try:
            payload = strict_json(path)
        except SourceGuardError:
            continue
        if isinstance(payload, dict) and canonical_url(str(payload.get("SHOW_URL") or "")) == identity["canonical_url"]:
            return path.parent.name, "Bloomberg page URL exists in an earlier published output"
    return "", ""


def clip_words(clip: Any) -> list[str]:
    if not isinstance(clip, dict) or not isinstance(clip.get("subtitles"), list):
        return []
    text = " ".join(
        str(item.get("en") or "")
        for item in clip["subtitles"]
        if isinstance(item, dict)
    )
    return normalize_words(text)


def shingle_coverage(needle: list[str], haystack: set[tuple[str, ...]], size: int = 4) -> float:
    if len(needle) < size:
        return 0.0
    shingles = [tuple(needle[index : index + size]) for index in range(len(needle) - size + 1)]
    return sum(item in haystack for item in shingles) / len(shingles)


def bootstrap_plan_duplicate(
    identity: dict[str, Any], transcript: dict[str, Any], rendered_root: Path
) -> tuple[str, str]:
    words = normalize_words(transcript_text(transcript))
    if len(words) < 200 or not rendered_root.exists():
        return "", ""
    full_shingles = {tuple(words[index : index + 4]) for index in range(len(words) - 3)}
    for path in sorted(rendered_root.glob("*/highlight_plan.json"), reverse=True):
        previous_date = path.parent.name
        if not DATE_DIR_RE.fullmatch(previous_date) or previous_date == identity["show_date"]:
            continue
        try:
            payload = strict_json(path)
        except SourceGuardError:
            continue
        clips = payload.get("clips") if isinstance(payload, dict) else None
        if not isinstance(clips, list):
            continue
        matched_word_count = 0
        matched_clips = 0
        eligible_clips = 0
        for clip in clips:
            words_in_clip = clip_words(clip)
            if len(words_in_clip) < 45:
                continue
            eligible_clips += 1
            if shingle_coverage(words_in_clip, full_shingles) >= 0.82:
                matched_clips += 1
                matched_word_count += len(words_in_clip)
        if matched_clips >= 2 and matched_word_count >= 180:
            return previous_date, f"transcript contains {matched_clips} previously published highlight segments"
        if eligible_clips == 1 and matched_clips == 1 and matched_word_count >= 160:
            return previous_date, "transcript contains the complete previously published highlight"
    return "", ""


def emit_outputs(path: Path | None, *, duplicate: bool, reason: str, matched_date: str) -> None:
    if path is None:
        return
    safe_reason = re.sub(r"[\r\n]+", " ", reason).strip()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"duplicate={'true' if duplicate else 'false'}\n")
        handle.write(f"reason={safe_reason}\n")
        handle.write(f"matched_date={matched_date}\n")


def check_command(args: argparse.Namespace) -> int:
    metadata = strict_json(args.show_metadata)
    plan = strict_json(args.download_plan)
    manifest = optional_manifest(args.manifest)
    transcript = strict_json(args.transcript) if args.transcript else None
    if not isinstance(metadata, dict) or not isinstance(plan, dict) or not isinstance(manifest, dict):
        raise SourceGuardError("show metadata, download plan and manifest must be JSON objects")
    identity = validate_identity(build_identity(metadata, plan, manifest, transcript))
    write_json_atomic(args.identity_output, identity)

    reason = validate_manifest(manifest, identity["show_date"], identity["show_type"]) if manifest else ""
    matched_date = ""
    if reason:
        manifest_dates = dates_in_text(f"{identity['manifest_title']} {canonical_url(str(plan.get('url') or ''))}")
        matched_date = sorted(manifest_dates)[0] if manifest_dates else ""
    else:
        ledger = load_ledger(args.ledger)
        matched_date, reason = ledger_duplicate(identity, ledger)
    if not reason:
        matched_date, reason = bootstrap_url_duplicate(identity, args.rendered_root)
    if not reason and transcript is not None:
        matched_date, reason = bootstrap_plan_duplicate(identity, transcript, args.rendered_root)

    duplicate = bool(reason)
    emit_outputs(args.github_output, duplicate=duplicate, reason=reason, matched_date=matched_date)
    if duplicate:
        print(f"::warning::Skipping stale or duplicate Bloomberg source: {reason}")
    else:
        phase = "transcript" if transcript is not None else "asset"
        print(f"Bloomberg {phase} source is new for {identity['show_date']}.")
    return 0


def record_command(args: argparse.Namespace) -> int:
    identity = validate_identity(strict_json(args.identity), require_transcript=True)
    ledger = load_ledger(args.ledger)
    record = {
        key: identity[key]
        for key in (
            "show_date",
            "show_type",
            "canonical_url",
            "asset_id",
            "duration",
            "word_count",
            "transcript_sha256",
            "transcript_simhash",
        )
    }
    record["processed_at"] = args.processed_at or utc_timestamp()
    parse_timestamp(record["processed_at"], context="processed_at")
    sources = [item for item in ledger["sources"] if item["show_date"] != identity["show_date"]]
    sources.append(record)
    sources.sort(key=lambda item: item["show_date"])
    updated = {"schema_version": SCHEMA_VERSION, "sources": sources}
    validate_ledger(updated, source=args.ledger)
    if updated == ledger:
        print(f"Show source already recorded for {identity['show_date']}.")
        return 0
    write_json_atomic(args.ledger, updated)
    print(f"Recorded show source for {identity['show_date']} in {args.ledger}.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    check = subparsers.add_parser("check", help="Check one downloaded source and write a sidecar identity.")
    check.add_argument("--show-metadata", type=Path, required=True)
    check.add_argument("--download-plan", type=Path, required=True)
    check.add_argument("--manifest", type=Path, required=True)
    check.add_argument("--transcript", type=Path)
    check.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    check.add_argument("--rendered-root", type=Path, default=Path("rendered-clips"))
    check.add_argument("--identity-output", type=Path, required=True)
    check.add_argument("--github-output", type=Path)
    check.set_defaults(func=check_command)

    record = subparsers.add_parser("record", help="Record a successfully processed source.")
    record.add_argument("--identity", type=Path, required=True)
    record.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER)
    record.add_argument("--processed-at", default="")
    record.set_defaults(func=record_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except SourceGuardError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    raise SystemExit(main())

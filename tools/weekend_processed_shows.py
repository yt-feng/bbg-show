#!/usr/bin/env python3
"""Validate and update the terminal-state ledger for Bloomberg Weekend shows."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"rendered", "no_eligible_speakers", "no_eligible_clips"})
DEFAULT_PATH = Path("rendered-clips/weekend/processed_shows.json")
ROOT_KEYS = frozenset({"schema_version", "shows"})
SHOW_KEYS = frozenset({"show_date", "status", "processed_at"})


class WeekendProcessedShowsError(ValueError):
    """Raised when a weekend processed-shows ledger is malformed."""


@dataclass(frozen=True)
class RecordResult:
    ledger: dict[str, Any]
    changed: bool


def empty_ledger() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "shows": []}


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise WeekendProcessedShowsError(f"Duplicate JSON object key: {key!r}")
        payload[key] = value
    return payload


def parse_show_date(value: Any, *, context: str = "show_date") -> str:
    if not isinstance(value, str):
        raise WeekendProcessedShowsError(f"{context} must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise WeekendProcessedShowsError(f"{context} must be a valid YYYY-MM-DD date: {value!r}") from exc
    if parsed.isoformat() != value:
        raise WeekendProcessedShowsError(f"{context} must use canonical YYYY-MM-DD format: {value!r}")
    return value


def format_utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise WeekendProcessedShowsError("processed_at must include timezone information")
    normalized = value.astimezone(timezone.utc)
    timespec = "microseconds" if normalized.microsecond else "seconds"
    return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")


def parse_utc_timestamp(value: Any, *, context: str = "processed_at") -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise WeekendProcessedShowsError(f"{context} must be a canonical UTC timestamp ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise WeekendProcessedShowsError(f"{context} is not a valid timestamp: {value!r}") from exc
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise WeekendProcessedShowsError(f"{context} must be in UTC")
    if format_utc_timestamp(parsed) != value:
        raise WeekendProcessedShowsError(f"{context} must use canonical UTC format: {value!r}")
    return parsed


def normalize_processed_at(value: str | datetime | None) -> str:
    if value is None:
        return format_utc_timestamp(datetime.now(timezone.utc))
    if isinstance(value, datetime):
        return format_utc_timestamp(value)
    parse_utc_timestamp(value)
    return value


def validate_ledger(payload: Any, *, source: Path | str = "ledger") -> dict[str, Any]:
    label = str(source)
    if not isinstance(payload, dict):
        raise WeekendProcessedShowsError(f"{label}: root must be an object")
    if frozenset(payload) != ROOT_KEYS:
        missing = sorted(ROOT_KEYS - frozenset(payload))
        extra = sorted(frozenset(payload) - ROOT_KEYS)
        raise WeekendProcessedShowsError(f"{label}: invalid root keys; missing={missing}, extra={extra}")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != SCHEMA_VERSION:
        raise WeekendProcessedShowsError(
            f"{label}: schema_version must be integer {SCHEMA_VERSION}, got {payload['schema_version']!r}"
        )

    shows = payload["shows"]
    if not isinstance(shows, list):
        raise WeekendProcessedShowsError(f"{label}: shows must be an array")

    seen_dates: set[str] = set()
    previous_date = ""
    for index, item in enumerate(shows):
        context = f"{label}: shows[{index}]"
        if not isinstance(item, dict):
            raise WeekendProcessedShowsError(f"{context} must be an object")
        if frozenset(item) != SHOW_KEYS:
            missing = sorted(SHOW_KEYS - frozenset(item))
            extra = sorted(frozenset(item) - SHOW_KEYS)
            raise WeekendProcessedShowsError(f"{context} has invalid keys; missing={missing}, extra={extra}")

        show_date = parse_show_date(item["show_date"], context=f"{context}.show_date")
        status = item["status"]
        if not isinstance(status, str) or status not in TERMINAL_STATUSES:
            raise WeekendProcessedShowsError(
                f"{context}.status must be one of {sorted(TERMINAL_STATUSES)}, got {status!r}"
            )
        parse_utc_timestamp(item["processed_at"], context=f"{context}.processed_at")
        if show_date in seen_dates:
            raise WeekendProcessedShowsError(f"{label}: duplicate show_date: {show_date}")
        if previous_date and show_date < previous_date:
            raise WeekendProcessedShowsError(f"{label}: shows must be sorted by show_date")
        seen_dates.add(show_date)
        previous_date = show_date

    return payload


def load_processed_shows(path: Path) -> dict[str, Any]:
    if not path.exists():
        return empty_ledger()
    if not path.is_file():
        raise WeekendProcessedShowsError(f"Weekend processed-shows path is not a file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=strict_json_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WeekendProcessedShowsError(f"Cannot read valid JSON from {path}: {exc}") from exc
    return validate_ledger(payload, source=path)


def show_records(ledger: dict[str, Any]) -> dict[str, dict[str, str]]:
    validate_ledger(ledger)
    return {str(item["show_date"]): item for item in ledger["shows"]}


def processed_show_dates(ledger: dict[str, Any]) -> set[str]:
    return set(show_records(ledger))


def rendered_processed_at(ledger: dict[str, Any]) -> dict[str, datetime]:
    return {
        show_date: parse_utc_timestamp(item["processed_at"])
        for show_date, item in show_records(ledger).items()
        if item["status"] == "rendered"
    }


def write_processed_shows(path: Path, ledger: dict[str, Any]) -> None:
    validate_ledger(ledger, source=path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(ledger, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def record_processed_show(
    path: Path,
    show_date: str,
    status: str,
    *,
    processed_at: str | datetime | None = None,
    refresh: bool = False,
) -> RecordResult:
    show_date = parse_show_date(show_date)
    if status not in TERMINAL_STATUSES:
        raise WeekendProcessedShowsError(f"status must be one of {sorted(TERMINAL_STATUSES)}, got {status!r}")
    if refresh and status != "rendered":
        raise WeekendProcessedShowsError("refresh is only valid when recording rendered status")
    timestamp = normalize_processed_at(processed_at)
    ledger = load_processed_shows(path)
    records = show_records(ledger)
    existing = records.get(show_date)

    if existing is not None:
        if existing["status"] == status:
            if not refresh:
                return RecordResult(ledger=ledger, changed=False)
        if existing["status"] == "rendered":
            if status != "rendered" or not refresh:
                return RecordResult(ledger=ledger, changed=False)

    replacement = {"show_date": show_date, "status": status, "processed_at": timestamp}
    updated = [item for item in ledger["shows"] if item["show_date"] != show_date]
    updated.append(replacement)
    updated.sort(key=lambda item: item["show_date"])
    next_ledger = {"schema_version": SCHEMA_VERSION, "shows": updated}
    write_processed_shows(path, next_ledger)
    return RecordResult(ledger=next_ledger, changed=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record_parser = subparsers.add_parser("record", help="Record a terminal result for one Weekend show.")
    record_parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    record_parser.add_argument("--show-date", required=True)
    record_parser.add_argument("--status", choices=sorted(TERMINAL_STATUSES), required=True)
    record_parser.add_argument("--processed-at", default="", help="Canonical UTC timestamp; defaults to now.")
    record_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh processed_at for a successfully re-rendered show.",
    )

    validate_parser = subparsers.add_parser("validate", help="Validate an existing ledger.")
    validate_parser.add_argument("--path", type=Path, default=DEFAULT_PATH)

    args = parser.parse_args()
    try:
        if args.command == "validate":
            ledger = load_processed_shows(args.path)
            print(f"Valid weekend processed-shows ledger: {args.path} ({len(ledger['shows'])} show(s))")
            return

        result = record_processed_show(
            args.path,
            args.show_date,
            args.status,
            processed_at=args.processed_at or None,
            refresh=args.refresh,
        )
    except WeekendProcessedShowsError as exc:
        raise SystemExit(str(exc)) from exc

    action = "Recorded" if result.changed else "Already terminal"
    record = show_records(result.ledger)[args.show_date]
    print(f"{action}: {record['show_date']} {record['status']} at {record['processed_at']}")


if __name__ == "__main__":
    main()

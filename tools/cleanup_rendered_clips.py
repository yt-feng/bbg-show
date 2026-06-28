#!/usr/bin/env python3
"""Remove rendered clip date folders older than a retention window."""

from __future__ import annotations

import argparse
import re
import shutil
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DEFAULT_TARGETS = (
    Path("rendered-clips"),
    Path("rendered-clips/top-videos"),
)


def parse_now(value: str, timezone: ZoneInfo) -> datetime:
    if not value:
        return datetime.now(timezone)

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"

    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def date_dir_start(name: str, timezone: ZoneInfo) -> datetime | None:
    if not DATE_DIR_RE.match(name):
        return None

    try:
        date_value = datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return None
    return datetime.combine(date_value, time.min, tzinfo=timezone)


def expired_date_dirs(root: Path, cutoff: datetime, timezone: ZoneInfo) -> list[Path]:
    if not root.exists():
        print(f"Skip missing target: {root}")
        return []

    if not root.is_dir():
        raise SystemExit(f"Cleanup target is not a directory: {root}")

    expired: list[Path] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue

        started_at = date_dir_start(child.name, timezone)
        if started_at is None:
            continue

        if started_at < cutoff:
            expired.append(child)

    return expired


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        action="append",
        type=Path,
        default=[],
        help="Directory containing YYYY-MM-DD rendered clip folders. Can be passed more than once.",
    )
    parser.add_argument("--retention-hours", type=int, default=72)
    parser.add_argument("--timezone", default="Asia/Shanghai")
    parser.add_argument(
        "--now",
        default="",
        help="Override current time for tests, e.g. 2026-06-22T08:00:00+08:00.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.retention_hours < 1:
        raise SystemExit("--retention-hours must be at least 1")

    timezone = ZoneInfo(args.timezone)
    now = parse_now(args.now, timezone)
    cutoff = now - timedelta(hours=args.retention_hours)
    targets = args.target or list(DEFAULT_TARGETS)

    print(f"Now: {now.isoformat()}")
    print(f"Retention: {args.retention_hours} hours")
    print(f"Cutoff: {cutoff.isoformat()}")

    removed = 0
    for target in targets:
        for path in expired_date_dirs(target, cutoff, timezone):
            print(f"{'Would remove' if args.dry_run else 'Removing'}: {path}")
            if not args.dry_run:
                shutil.rmtree(path)
            removed += 1

    if args.dry_run:
        print(f"Expired rendered clip directories found: {removed}")
    else:
        print(f"Expired rendered clip directories removed: {removed}")


if __name__ == "__main__":
    main()

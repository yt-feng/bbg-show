#!/usr/bin/env python3
"""Reject plaintext or unexpected files in the persistent transcript archive."""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path


CATEGORIES = {"shows", "top-videos", "ark-invest"}
ARCHIVE_NAME_RE = re.compile(
    r"^[0-9a-f]{64}__[0-9a-f]{64}\.(?:json|md)\.cms$"
)


class ArchiveTreeError(ValueError):
    """Raised when the persistent archive tree contains an unsafe path."""


def _valid_date(value: str) -> bool:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return False
    return parsed.isoformat() == value


def verify_tree(root: Path) -> int:
    archive_root = Path(root)
    if not archive_root.is_dir() or archive_root.is_symlink():
        raise ArchiveTreeError("Transcript archive root must be a real directory")

    archive_count = 0
    archive_pairs: dict[tuple[str, str, str], set[str]] = {}
    for path in sorted(archive_root.rglob("*")):
        relative = path.relative_to(archive_root)
        parts = relative.parts
        if path.is_symlink():
            raise ArchiveTreeError(f"Symlinks are not allowed: {relative}")

        if path.is_dir():
            if len(parts) == 1 and parts[0] in CATEGORIES:
                continue
            if len(parts) == 2 and parts[0] in CATEGORIES and _valid_date(parts[1]):
                continue
            raise ArchiveTreeError(f"Unexpected transcript archive directory: {relative}")

        if not path.is_file():
            raise ArchiveTreeError(f"Unexpected transcript archive entry: {relative}")
        if relative == Path("README.md"):
            continue
        if (
            len(parts) != 3
            or parts[0] not in CATEGORIES
            or not _valid_date(parts[1])
            or ARCHIVE_NAME_RE.fullmatch(parts[2]) is None
        ):
            raise ArchiveTreeError(f"Plaintext or unexpected transcript archive file: {relative}")
        if path.stat().st_size <= 0:
            raise ArchiveTreeError(f"Encrypted transcript archive is empty: {relative}")
        document_kind = "json" if parts[2].endswith(".json.cms") else "md"
        basename = parts[2].removesuffix(f".{document_kind}.cms")
        archive_pairs.setdefault((parts[0], parts[1], basename), set()).add(document_kind)
        archive_count += 1

    incomplete = [
        "/".join(key)
        for key, kinds in sorted(archive_pairs.items())
        if kinds != {"json", "md"}
    ]
    if incomplete:
        raise ArchiveTreeError(
            "Encrypted transcript archive pair is incomplete: " + ", ".join(incomplete)
        )
    return archive_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("transcripts"))
    args = parser.parse_args()
    try:
        count = verify_tree(args.root)
    except ArchiveTreeError as exc:
        raise SystemExit(f"Transcript archive verification failed: {exc}") from None
    print(f"Verified {count} encrypted transcript archive file(s).", flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Audit or consolidate the four known Jianguoyun delivery folders.

This is a manually dispatched, cloud-only migration. It does not expire videos,
upload local files, change credentials, or traverse unrelated root folders.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request

from sync_rendered_clips_to_jianguoyun import (
    JianguoyunSyncError,
    WebDavTarget,
    parse_iso_date,
    require_env,
)


HOME = ("我的坚果云",)
CANONICAL = (*HOME, "KC Desk Notes", "Ops")
LEGACY_ROOTS = ((*HOME, "KCdesk"), (*HOME, "Portal Suite"), (*HOME, "kc娱乐"))
CATEGORIES = ("BBG Show", "BBG Top Videos", "Portal 娱乐")
ALIASES = {**{name: name for name in CATEGORIES}, "KC娱乐": "Portal 娱乐", "kc娱乐": "Portal 娱乐"}
KNOWN_ROOTS = ((*HOME, "KC Desk Notes"), *LEGACY_ROOTS)
PROPFIND = b'''<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getetag/></d:prop></d:propfind>'''
PathParts = tuple[str, ...]


def display(path: PathParts) -> str:
    return "/" + "/".join(path)


def emit(event: str, **fields: object) -> None:
    print(event + " " + json.dumps(fields, ensure_ascii=False, sort_keys=True), flush=True)


@dataclass(frozen=True)
class Entry:
    path: PathParts
    is_dir: bool
    size: int = -1
    etag: str = ""


class MigrationDav:
    """Small WebDavTarget adapter; every listing uses Depth:1 and a scan budget."""

    def __init__(self, target: WebDavTarget, *, max_entries: int = 20000) -> None:
        self.target = target
        self.max_entries = max_entries
        self.entries_seen = 0

    def _entry(self, response: ET.Element) -> Entry | None:
        href = response.findtext("{DAV:}href", "")
        parsed = urlparse(href)
        base = urlparse(self.target.base_url)
        if parsed.netloc and parsed.netloc != base.netloc:
            raise JianguoyunSyncError("PROPFIND returned an unexpected host")
        base_path = unquote(base.path).rstrip("/") + "/"
        decoded = unquote(parsed.path).rstrip("/")
        if not decoded.startswith(base_path):
            raise JianguoyunSyncError("PROPFIND returned a path outside the WebDAV endpoint")
        parts = tuple(decoded[len(base_path):].split("/"))
        if any(not part or part in {".", ".."} or "\x00" in part for part in parts):
            raise JianguoyunSyncError("PROPFIND returned an invalid path")
        prop = None
        for propstat in response.findall("{DAV:}propstat"):
            if " 200 " in propstat.findtext("{DAV:}status", ""):
                prop = propstat.find("{DAV:}prop")
                break
        if prop is None:
            return None
        is_dir = prop.find("{DAV:}resourcetype/{DAV:}collection") is not None
        try:
            size = int(prop.findtext("{DAV:}getcontentlength", "-1"))
        except ValueError:
            size = -1
        return Entry(parts, is_dir, size, prop.findtext("{DAV:}getetag", ""))

    def listing(self, path: PathParts, *, depth: int = 1) -> tuple[Entry | None, list[Entry]]:
        status, _, body = self.target._request(
            "PROPFIND", list(path), accepted={207, 404},
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            data_factory=lambda: PROPFIND, attempts=2,
        )
        if status == 404:
            return None, []
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise JianguoyunSyncError("Invalid XML in PROPFIND response") from exc
        own = None
        children = []
        for response in root.findall("{DAV:}response"):
            entry = self._entry(response)
            if entry is None:
                continue
            self.entries_seen += 1
            if self.entries_seen > self.max_entries:
                raise JianguoyunSyncError("Migration scan limit reached; rerun with --date YYYY-MM-DD")
            if entry.path == path:
                own = entry
            elif depth == 1 and entry.path[:-1] == path:
                children.append(entry)
            else:
                raise JianguoyunSyncError("PROPFIND returned a non-child path")
        if own is None:
            raise JianguoyunSyncError("PROPFIND did not describe the requested path")
        return own, sorted(children, key=lambda item: item.path)

    def stat(self, path: PathParts) -> Entry | None:
        return self.listing(path, depth=0)[0]

    def walk(self, path: PathParts, *, level: int = 0) -> tuple[list[Entry], list[PathParts]]:
        if level > 12:
            raise JianguoyunSyncError("Migration nesting limit reached")
        own, children = self.listing(path)
        if own is None:
            return [], []
        if not own.is_dir:
            raise JianguoyunSyncError(f"Expected a folder at {display(path)}")
        files, dirs = [], [path]
        for entry in children:
            if entry.is_dir:
                nested_files, nested_dirs = self.walk(entry.path, level=level + 1)
                files.extend(nested_files)
                dirs.extend(nested_dirs)
            else:
                files.append(entry)
        return files, dirs

    def ensure(self, path: PathParts) -> None:
        if path[:len(CANONICAL)] != CANONICAL:
            raise JianguoyunSyncError("Destination must be inside KC Desk Notes/Ops")
        self.target.ensure_collection(path[len(CANONICAL):])

    def sha256(self, path: PathParts) -> tuple[str, str]:
        request = Request(self.target.url(path), method="GET")
        request.add_unredirected_header("Authorization", self.target.authorization)
        digest = hashlib.sha256()
        try:
            with self.target.opener(request, timeout=120) as response:
                if response.status != 200:
                    raise JianguoyunSyncError(f"GET failed (HTTP {response.status})")
                etag = response.headers.get("ETag", "")
                while chunk := response.read(1024 * 1024):
                    digest.update(chunk)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise JianguoyunSyncError(f"GET failed (HTTP {status})") from None
        except (URLError, TimeoutError, OSError) as exc:
            raise JianguoyunSyncError(f"GET failed ({type(exc).__name__})") from None
        return digest.hexdigest(), etag

    def move(self, source: PathParts, destination: PathParts) -> bool:
        status, _, _ = self.target._request(
            "MOVE", list(source), accepted={201, 204, 412},
            headers={"Destination": self.target.url(destination), "Overwrite": "F"},
            attempts=1, timeout=120,
        )
        return status != 412

    def delete(self, path: PathParts, *, etag: str = "") -> bool:
        status, _, _ = self.target._request(
            "DELETE", list(path), accepted={200, 204, 404, 412},
            headers={"If-Match": etag} if etag else {}, attempts=1,
        )
        return status != 412


@dataclass
class Collection:
    source: PathParts
    date: str
    category: str
    files: list[Entry]
    directories: list[PathParts]

    @property
    def destination(self) -> PathParts:
        return (*CANONICAL, self.date, self.category)


def selected_date(name: str, date_filter: str | None) -> bool:
    try:
        parse_iso_date(name)
    except JianguoyunSyncError:
        return False
    return date_filter is None or name == date_filter


def inventory(dav: MigrationDav, date_filter: str | None, phase: str) -> tuple[list[Collection], dict[str, bool]]:
    _, top = dav.listing(HOME)
    known = {item.path[-1]: item for item in top if item.path in KNOWN_ROOTS}
    emit("JIANGUOYUN_ROOTS", phase=phase, known=[name for name in sorted(known)],
         other_roots=sum(item.path not in KNOWN_ROOTS for item in top))
    collections = []
    presence = {}
    for root in KNOWN_ROOTS:
        own, root_children = dav.listing(root)
        presence[root[-1]] = own is not None
        emit("JIANGUOYUN_ROOT", phase=phase, root=root[-1], exists=own is not None,
             children=[item.path[-1] for item in root_children])
        if own is None:
            continue
        if root == LEGACY_ROOTS[2]:
            dates = root_children
        else:
            _, dates = dav.listing((*root, "Ops"))
        for date_entry in dates:
            if not date_entry.is_dir or not selected_date(date_entry.path[-1], date_filter):
                continue
            date = date_entry.path[-1]
            if root == LEGACY_ROOTS[2]:
                files, dirs = dav.walk(date_entry.path)
                collections.append(Collection(date_entry.path, date, "Portal 娱乐", files, dirs))
                continue
            _, category_entries = dav.listing(date_entry.path)
            unknown = [item.path[-1] for item in category_entries if not item.is_dir or item.path[-1] not in ALIASES]
            if unknown:
                emit("JIANGUOYUN_UNMAPPED", phase=phase, path=display(date_entry.path), entries=unknown)
            for category_entry in category_entries:
                if category_entry.is_dir and category_entry.path[-1] in ALIASES:
                    files, dirs = dav.walk(category_entry.path)
                    collections.append(Collection(category_entry.path, date, ALIASES[category_entry.path[-1]], files, dirs))
    for collection in collections:
        emit("JIANGUOYUN_COLLECTION", phase=phase, path=display(collection.source),
             date=collection.date, category=collection.category,
             mp4=sum(item.path[-1].lower().endswith(".mp4") for item in collection.files),
             files=len(collection.files), destination=display(collection.destination))
    dates = sorted({item.date for item in collections} | ({date_filter} if date_filter else set()))
    for date in dates:
        counts = {category: sum(
            entry.path[-1].lower().endswith(".mp4")
            for collection in collections
            if collection.date == date and collection.source == (*CANONICAL, date, category)
            for entry in collection.files
        ) for category in CATEGORIES}
        emit("JIANGUOYUN_CANONICAL_COUNTS", phase=phase, date=date, **counts)
    return collections, presence


def collision_path(destination: PathParts, source: PathParts, attempt: int = 0) -> PathParts:
    name = PurePosixPath(destination[-1])
    marker = hashlib.sha256(display(source).encode("utf-8")).hexdigest()[:12]
    suffix = f"__from_{marker}" + (f"_{attempt}" if attempt else "")
    stem = name.stem
    while len(f"{stem}{suffix}{name.suffix}".encode("utf-8")) > 240 and stem:
        stem = stem[:-1]
    return (*destination[:-1], f"{stem}{suffix}{name.suffix}")


def migrate_file(dav: MigrationDav, source: Entry, destination: PathParts, stats: dict[str, int]) -> None:
    candidate = destination
    source_digest = None
    for attempt in range(12):
        existing = dav.stat(candidate)
        if existing is None:
            dav.ensure(candidate[:-1])
            if dav.move(source.path, candidate):
                stats["moved"] += 1
                emit("JIANGUOYUN_MOVED", source=display(source.path), destination=display(candidate))
                return
            # Another writer created the destination; compare before taking action.
            continue
        if not existing.is_dir:
            if source_digest is None:
                source_digest, source_etag = dav.sha256(source.path)
            target_digest, target_etag = dav.sha256(candidate)
            if source_digest == target_digest:
                source_guard = source_etag or source.etag
                target_guard = target_etag or existing.etag
                if source_guard and target_guard:
                    current = dav.stat(candidate)
                    if current is None or current.is_dir or current.etag != target_guard:
                        raise JianguoyunSyncError("Destination changed while verifying a duplicate")
                    # Guard the source against changes after hashing. If the
                    # provider supplies no validators, retain both files instead.
                    if not dav.delete(source.path, etag=source_guard):
                        raise JianguoyunSyncError("Source changed while removing a verified duplicate")
                    stats["duplicates"] += 1
                    emit("JIANGUOYUN_DUPLICATE_REMOVED", source=display(source.path), destination=display(candidate), verified="sha256")
                    return
                emit("JIANGUOYUN_DUPLICATE_PRESERVED", source=display(source.path), reason="provider omitted ETag")
        candidate = collision_path(destination, source.path, attempt)
    raise JianguoyunSyncError("Could not allocate a non-overwriting destination after 12 attempts")


def remove_empty(dav: MigrationDav, path: PathParts, stats: dict[str, int]) -> None:
    own, children = dav.listing(path)
    if own is None:
        return
    for child in children:
        if not child.is_dir and child.path[-1] == ".DS_Store":
            if not dav.delete(child.path, etag=child.etag):
                raise JianguoyunSyncError("Metadata changed during cleanup")
            stats["metadata_removed"] += 1
    own, children = dav.listing(path)
    if own is not None and not children:
        if not dav.delete(path, etag=own.etag):
            raise JianguoyunSyncError("Folder changed during empty-folder cleanup")
        stats["empty_folders_removed"] += 1
        emit("JIANGUOYUN_EMPTY_FOLDER_REMOVED", path=display(path))


def consolidate(dav: MigrationDav, *, date_filter: str | None = None, apply: bool = False) -> dict[str, int]:
    if date_filter:
        parse_iso_date(date_filter)
    collections, _ = inventory(dav, date_filter, "before")
    stats = {"moved": 0, "duplicates": 0, "metadata_removed": 0, "empty_folders_removed": 0}
    if not apply:
        emit("JIANGUOYUN_AUDIT_COMPLETE", planned_files=sum(
            item.path[-1] != ".DS_Store" for collection in collections
            if collection.source != collection.destination for item in collection.files))
        return stats
    cleanup = set()
    for collection in collections:
        if collection.source == collection.destination:
            continue
        for entry in collection.files:
            if entry.path[-1] == ".DS_Store":
                if not dav.delete(entry.path, etag=entry.etag):
                    raise JianguoyunSyncError("Metadata changed during cleanup")
                stats["metadata_removed"] += 1
            else:
                relative = entry.path[len(collection.source):]
                migrate_file(dav, entry, (*collection.destination, *relative), stats)
        cleanup.update(collection.directories)
    # Clean selected empty dates even when there were no recognized categories.
    for root in LEGACY_ROOTS:
        parent = root if root == LEGACY_ROOTS[2] else (*root, "Ops")
        _, dates = dav.listing(parent)
        cleanup.update(item.path for item in dates if item.is_dir and selected_date(item.path[-1], date_filter))
        cleanup.add(parent)
        cleanup.add(root)
    for path in sorted(cleanup, key=lambda item: (-len(item), item)):
        remove_empty(dav, path, stats)
    _, roots = inventory(dav, date_filter, "after")
    emit("JIANGUOYUN_CONSOLIDATION_COMPLETE", **stats,
         legacy_roots_remaining=[name for name, exists in roots.items() if exists and name != "KC Desk Notes"])
    return stats


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply migration; omission performs read-only audit.")
    parser.add_argument("--date", help="Only migrate this strict YYYY-MM-DD date; omission selects all dates.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.date:
        parse_iso_date(args.date)
    target = WebDavTarget(require_env("JIANGUOYUN_WEBDAV_URL"),
                          require_env("JIANGUOYUN_WEBDAV_USERNAME"),
                          require_env("JIANGUOYUN_WEBDAV_PASSWORD"), display(CANONICAL))
    consolidate(MigrationDav(target), date_filter=args.date, apply=args.apply)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except JianguoyunSyncError as exc:
        emit("JIANGUOYUN_CONSOLIDATION_FAILED", error=str(exc))
        raise SystemExit(1) from None

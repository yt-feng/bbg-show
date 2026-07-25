#!/usr/bin/env python3
"""Idempotently upload rendered Bloomberg clips to Jianguoyun WebDAV."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


DEFAULT_REMOTE_ROOT = "/我的坚果云/KCdesk/Ops"
KIND_CATEGORIES = {
    "show": "BBG Show",
    "top-videos": "BBG Top Videos",
}
TRANSIENT_HTTP_STATUSES = {408, 409, 423, 425, 429, 500, 502, 503, 504}
UPLOAD_SUCCESS_STATUSES = {200, 201, 204}
MAX_REMOTE_FILENAME_BYTES = 220
INVALID_FILENAME_RE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class JianguoyunSyncError(RuntimeError):
    """Raised for a safe-to-log WebDAV synchronization failure."""


def parse_iso_date(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise JianguoyunSyncError("date must use strict YYYY-MM-DD format") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise JianguoyunSyncError("date must use strict YYYY-MM-DD format")
    return parsed


def remote_date_for(kind: str, source_date: str) -> str:
    parsed = parse_iso_date(source_date)
    if kind == "show":
        parsed += timedelta(days=1)
    elif kind != "top-videos":
        raise JianguoyunSyncError(f"Unsupported sync kind: {kind}")
    return parsed.strftime("%Y-%m-%d")


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise JianguoyunSyncError(f"Missing required environment variable: {name}")
    return value


def discover_mp4_files(source_dir: Path) -> list[Path]:
    if not source_dir.exists():
        return []
    if not source_dir.is_dir():
        raise JianguoyunSyncError(f"Source path is not a directory: {source_dir}")
    files = sorted(
        (
            path
            for path in source_dir.rglob("*")
            if path.is_file() and path.suffix.lower() == ".mp4"
        ),
        key=lambda path: str(path.relative_to(source_dir)).casefold(),
    )
    for path in files:
        if path.stat().st_size <= 0:
            raise JianguoyunSyncError(f"Refusing to upload empty MP4: {path.name}")
    return files


def git_blob_sha(path: Path) -> str:
    size = path.stat().st_size
    digest = hashlib.sha1()
    digest.update(f"blob {size}\0".encode("ascii"))
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def clean_remote_filename(name: str, *, digest: str = "") -> str:
    cleaned = INVALID_FILENAME_RE.sub("_", name).strip(" .") or "video.mp4"
    suffix = Path(cleaned).suffix or ".mp4"
    stem = cleaned[: -len(suffix)] if cleaned.endswith(suffix) else cleaned
    digest_suffix = f"__{digest[:8]}" if digest else ""
    while len(f"{stem}{digest_suffix}{suffix}".encode("utf-8")) > MAX_REMOTE_FILENAME_BYTES and stem:
        stem = stem[:-1]
    return f"{stem}{digest_suffix}{suffix}"


def assign_remote_names(files: list[Path]) -> dict[Path, str]:
    cleaned_names = {path: clean_remote_filename(path.name) for path in files}
    counts: dict[str, int] = {}
    for name in cleaned_names.values():
        key = name.casefold()
        counts[key] = counts.get(key, 0) + 1
    return {
        path: clean_remote_filename(
            path.name,
            digest=git_blob_sha(path) if counts[cleaned_names[path].casefold()] > 1 else "",
        )
        for path in files
    }


def _status_of(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return int(status)


class WebDavTarget:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        remote_root: str = DEFAULT_REMOTE_ROOT,
        *,
        opener: Callable[..., Any] = urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise JianguoyunSyncError("WebDAV URL must be an HTTPS endpoint without embedded credentials")
        if parsed.hostname != "dav.jianguoyun.com":
            raise JianguoyunSyncError("WebDAV URL must use dav.jianguoyun.com")
        if parsed.query or parsed.fragment:
            raise JianguoyunSyncError("WebDAV URL must not include a query string or fragment")
        self.base_url = base_url.rstrip("/") + "/"
        self.root_parts = [part for part in remote_root.strip("/").split("/") if part]
        if not self.root_parts or any(part in {".", ".."} for part in self.root_parts):
            raise JianguoyunSyncError("Remote root must contain at least one path segment")
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self.authorization = f"Basic {token}"
        self.opener = opener
        self.sleeper = sleeper

    def url(self, parts: Iterable[str]) -> str:
        return self.base_url + "/".join(quote(part, safe="") for part in parts)

    def _request(
        self,
        method: str,
        parts: list[str],
        *,
        accepted: set[int],
        headers: Mapping[str, str] | None = None,
        data_factory: Callable[[], bytes | BinaryIO | None] | None = None,
        timeout: int = 60,
        attempts: int = 4,
    ) -> tuple[int, Mapping[str, str], bytes]:
        max_attempts = max(1, attempts)
        display_path = "/".join(parts)
        last_error = "unknown error"
        for attempt in range(max_attempts):
            data: bytes | BinaryIO | None = None
            retryable = True
            try:
                data = data_factory() if data_factory else None
                request = Request(
                    self.url(parts),
                    data=data,
                    method=method,
                    headers={"User-Agent": "bbg-show-jianguoyun-sync/1.0", **dict(headers or {})},
                )
                request.add_unredirected_header("Authorization", self.authorization)
                with self.opener(request, timeout=timeout) as response:
                    status = _status_of(response)
                    response_headers = response.headers
                    response_body = response.read()
                if status in accepted:
                    return status, response_headers, response_body
                last_error = f"HTTP {status}"
                retryable = status in TRANSIENT_HTTP_STATUSES
            except HTTPError as exc:
                status = exc.code
                response_headers = exc.headers or {}
                try:
                    response_body = exc.read()
                finally:
                    exc.close()
                if status in accepted:
                    return status, response_headers, response_body
                last_error = f"HTTP {status}"
                retryable = status in TRANSIENT_HTTP_STATUSES
            except (URLError, TimeoutError, OSError) as exc:
                last_error = type(exc).__name__
            finally:
                if data is not None and hasattr(data, "close"):
                    data.close()

            if not retryable or attempt + 1 >= max_attempts:
                break
            self.sleeper(min(2**attempt, 8))
        raise JianguoyunSyncError(f"{method} failed ({last_error}) for {display_path}")

    def ensure_collection(self, extra_parts: Iterable[str] = ()) -> None:
        current: list[str] = []
        for part in [*self.root_parts, *extra_parts]:
            current.append(part)
            self._request(
                "MKCOL",
                current,
                accepted={201, 405},
                timeout=45,
            )

    def remote_size(
        self,
        relative_parts: list[str],
        *,
        expected_size: int | None = None,
    ) -> int | None:
        parts = [*self.root_parts, *relative_parts]
        status, headers, _ = self._request(
            "HEAD",
            parts,
            accepted={200, 204, 404, 405},
            timeout=45,
        )
        if status == 404:
            return None
        if status == 200:
            try:
                size = int(headers.get("Content-Length", "-1"))
            except (TypeError, ValueError):
                size = -1
            if size >= 0 and (expected_size is None or size == expected_size):
                return size

        propfind_body = b"""<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:getcontentlength /></d:prop></d:propfind>"""
        status, _, body = self._request(
            "PROPFIND",
            parts,
            accepted={207, 404},
            headers={"Depth": "0", "Content-Type": "application/xml"},
            data_factory=lambda: propfind_body,
            timeout=60,
        )
        if status == 404:
            return None
        try:
            root = ET.fromstring(body)
            value = root.findtext(".//{DAV:}getcontentlength")
            return int(value) if value is not None else -1
        except (ET.ParseError, TypeError, ValueError) as exc:
            raise JianguoyunSyncError(
                f"Invalid PROPFIND response for {'/'.join(relative_parts)}"
            ) from exc

    def upload(self, local_path: Path, relative_parts: list[str]) -> None:
        size = local_path.stat().st_size
        self._request(
            "PUT",
            [*self.root_parts, *relative_parts],
            accepted=UPLOAD_SUCCESS_STATUSES,
            headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
            data_factory=lambda: local_path.open("rb"),
            timeout=900,
        )


def sync_files(
    target: WebDavTarget,
    *,
    files: list[Path],
    kind: str,
    remote_date: str,
) -> tuple[int, int, int]:
    category = KIND_CATEGORIES[kind]
    names = assign_remote_names(files)
    target.ensure_collection([remote_date, category])
    uploaded = 0
    skipped = 0
    failed = 0
    for index, path in enumerate(files, start=1):
        remote_parts = [remote_date, category, names[path]]
        try:
            size = path.stat().st_size
            existing_size = target.remote_size(
                remote_parts,
                expected_size=size,
            )
            if existing_size == size:
                skipped += 1
                print(
                    f"JIANGUOYUN_SKIP {index}/{len(files)} {'/'.join(remote_parts)}",
                    flush=True,
                )
                continue
            if existing_size is not None:
                print(
                    f"JIANGUOYUN_REPLACE {index}/{len(files)} {'/'.join(remote_parts)}",
                    flush=True,
                )
            else:
                print(
                    f"JIANGUOYUN_UPLOAD {index}/{len(files)} {'/'.join(remote_parts)}",
                    flush=True,
                )
            target.upload(path, remote_parts)
            verified_size: int | None = None
            for verify_attempt in range(4):
                verified_size = target.remote_size(
                    remote_parts,
                    expected_size=size,
                )
                if verified_size == size:
                    break
                if verify_attempt < 3:
                    target.sleeper(min(2**verify_attempt, 4))
            if verified_size != size:
                raise JianguoyunSyncError(
                    f"Uploaded size verification failed for {'/'.join(remote_parts)} "
                    f"(expected={size}, remote={verified_size})"
                )
            uploaded += 1
            print(
                f"JIANGUOYUN_UPLOADED {index}/{len(files)} {'/'.join(remote_parts)}",
                flush=True,
            )
        except (JianguoyunSyncError, OSError) as exc:
            failed += 1
            print(
                f"::warning::JIANGUOYUN_FILE_FAILED {index}/{len(files)} "
                f"{'/'.join(remote_parts)} ({exc})",
                flush=True,
            )
    return uploaded, skipped, failed


def run(args: argparse.Namespace) -> int:
    source_dir = Path(args.source_dir)
    files = discover_mp4_files(source_dir)
    remote_date = remote_date_for(args.kind, args.date)
    if not files:
        if args.allow_empty:
            print(
                f"JIANGUOYUN_NOOP kind={args.kind} date={remote_date} files=0",
                flush=True,
            )
            return 0
        raise JianguoyunSyncError(f"No MP4 files found under {source_dir}")

    print(
        f"JIANGUOYUN_SYNC_START kind={args.kind} date={remote_date} files={len(files)}",
        flush=True,
    )
    target = WebDavTarget(
        require_env("JIANGUOYUN_WEBDAV_URL"),
        require_env("JIANGUOYUN_WEBDAV_USERNAME"),
        require_env("JIANGUOYUN_WEBDAV_PASSWORD"),
        args.remote_root,
    )
    uploaded, skipped, failed = sync_files(
        target,
        files=files,
        kind=args.kind,
        remote_date=remote_date,
    )
    print(
        f"JIANGUOYUN_SYNC_COMPLETE uploaded={uploaded} skipped={skipped} failed={failed}",
        flush=True,
    )
    if failed:
        raise JianguoyunSyncError(
            f"{failed}/{len(files)} file(s) failed after remaining files were attempted"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=sorted(KIND_CATEGORIES), required=True)
    parser.add_argument("--date", required=True, help="Source/output date in YYYY-MM-DD.")
    parser.add_argument("--source-dir", required=True, help="Directory recursively containing MP4 files.")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--allow-empty", action="store_true")
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except JianguoyunSyncError as exc:
        print(f"JIANGUOYUN_SYNC_FAILED {exc}", flush=True)
        raise SystemExit(1) from exc

#!/usr/bin/env python3
"""Create content-addressed, encrypted JSON and Markdown transcript archives.

The only persistent outputs are DER CMS AuthEnvelopedData files.  Plaintext
archive documents are created inside a private temporary directory and removed
before this program returns.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ARCHIVE_FORMAT = "bbg-show-transcript-v1"


class ArchiveError(ValueError):
    """Raised when an encrypted transcript archive cannot be created safely."""


@dataclass(frozen=True)
class ArchiveResult:
    json_cms: Path
    markdown_cms: Path
    source_sha256: str
    transcript_sha256: str
    created: bool


def _reject_json_constant(_value: str) -> None:
    raise ArchiveError("JSON contains a non-finite number")


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ArchiveError("JSON contains a duplicate object key")
        value[key] = item
    return value


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ArchiveError(f"Unable to read {label}") from exc

    try:
        value = json.loads(
            raw,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except ValueError as exc:
        raise ArchiveError(f"{label.capitalize()} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ArchiveError(f"{label.capitalize()} must be a JSON object")
    return value


def _is_finite_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _validate_transcript(transcript: dict[str, Any]) -> None:
    segments = transcript.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ArchiveError("Transcript must contain a non-empty segments array")

    previous_start = -1.0
    for segment in segments:
        if not isinstance(segment, dict):
            raise ArchiveError("Every transcript segment must be a JSON object")
        start = segment.get("start")
        end = segment.get("end")
        text = segment.get("text")
        if not _is_finite_number(start) or not _is_finite_number(end):
            raise ArchiveError("Every transcript segment must have finite start and end times")
        if float(start) < 0 or float(end) < float(start):
            raise ArchiveError("Transcript segment times are invalid")
        if float(start) < previous_start:
            raise ArchiveError("Transcript segments must be ordered by start time")
        if not isinstance(text, str) or not text.strip():
            raise ArchiveError("Every transcript segment must contain text")
        previous_start = float(start)

    duration = transcript.get("duration")
    if duration is not None and (not _is_finite_number(duration) or float(duration) < 0):
        raise ArchiveError("Transcript duration must be a finite non-negative number")
    for field in ("model", "language"):
        if field in transcript and not isinstance(transcript[field], str):
            raise ArchiveError(f"Transcript {field} must be a string")


def _validate_source(source: dict[str, Any]) -> None:
    if not source:
        raise ArchiveError("Source metadata must not be empty")


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON used for hashes and the JSON archive."""

    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ArchiveError("Input contains a value that cannot be archived as JSON") from exc
    return rendered.encode("utf-8")


def json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _format_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"


def build_plaintext_documents(
    source: dict[str, Any],
    transcript: dict[str, Any],
    *,
    source_sha256: str,
    transcript_sha256: str,
) -> tuple[bytes, bytes]:
    """Build canonical JSON and readable Markdown plaintext documents."""

    archive = {
        "archive_format": ARCHIVE_FORMAT,
        "schema_version": 1,
        "source": source,
        "source_sha256": source_sha256,
        "transcript": transcript,
        "transcript_sha256": transcript_sha256,
    }
    json_document = canonical_json_bytes(archive) + b"\n"

    pretty_source = json.dumps(
        source,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    lines = [
        "# Video transcript",
        "",
        f"- Archive format: `{ARCHIVE_FORMAT}`",
        f"- Source SHA-256: `{source_sha256}`",
        f"- Transcript SHA-256: `{transcript_sha256}`",
        "",
        "## Source metadata",
        "",
    ]
    lines.extend(f"    {line}" for line in pretty_source.splitlines())
    lines.extend(["", "## Transcript", ""])
    for segment in transcript["segments"]:
        start = _format_timestamp(float(segment["start"]))
        end = _format_timestamp(float(segment["end"]))
        readable_text = " ".join(segment["text"].split())
        lines.append(f"    [{start} - {end}] {readable_text}")
    markdown_document = ("\n".join(lines) + "\n").encode("utf-8")
    return json_document, markdown_document


def _run_openssl(command: list[str], *, purpose: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            command,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Do not include OpenSSL output: keeping plaintext and source metadata out
        # of CI logs is more important than relaying provider diagnostics.
        raise ArchiveError(f"OpenSSL failed while {purpose}") from exc


def _validate_crypto_environment(
    recipient_cert: Path,
    *,
    openssl_binary: str,
) -> None:
    version_result = _run_openssl(
        [openssl_binary, "version"],
        purpose="checking the OpenSSL version",
    )
    version_text = version_result.stdout.decode("utf-8", errors="replace").strip()
    version_match = re.match(r"^OpenSSL\s+(\d+)\.", version_text)
    if version_match is None or int(version_match.group(1)) < 3:
        raise ArchiveError("OpenSSL 3 or newer is required for authenticated CMS encryption")

    certificate_result = _run_openssl(
        [
            openssl_binary,
            "x509",
            "-in",
            str(recipient_cert),
            "-noout",
            "-text",
        ],
        purpose="validating the recipient certificate",
    )
    certificate_text = certificate_result.stdout.decode("utf-8", errors="replace")
    bits_match = re.search(r"Public-Key:\s*\((\d+) bit\)", certificate_text)
    if (
        bits_match is None
        or int(bits_match.group(1)) < 3072
        or "Public Key Algorithm: rsaEncryption" not in certificate_text
        or "X509v3 Subject Key Identifier" not in certificate_text
    ):
        raise ArchiveError("Recipient certificate must contain an RSA key of at least 3072 bits and a key ID")


def _encrypt_cms(
    plaintext: Path,
    encrypted_output: Path,
    *,
    recipient_cert: Path,
    openssl_binary: str,
) -> None:
    _run_openssl(
        [
            openssl_binary,
            "cms",
            "-encrypt",
            "-binary",
            "-outform",
            "DER",
            "-in",
            str(plaintext),
            "-out",
            str(encrypted_output),
            "-aes-256-gcm",
            "-keyid",
            "-recip",
            str(recipient_cert),
            "-keyopt",
            "rsa_padding_mode:oaep",
            "-keyopt",
            "rsa_oaep_md:sha256",
            "-keyopt",
            "rsa_mgf1_md:sha256",
        ],
        purpose="encrypting an archive",
    )


def _validate_encrypted_cms(path: Path, *, openssl_binary: str) -> None:
    result = _run_openssl(
        [
            openssl_binary,
            "cms",
            "-cmsout",
            "-inform",
            "DER",
            "-in",
            str(path),
            "-print",
            "-noout",
        ],
        purpose="validating an encrypted archive",
    )
    structure = result.stdout.decode("utf-8", errors="replace")
    required_markers = (
        "id-smime-ct-authEnvelopedData",
        "subjectKeyIdentifier",
        "rsaesOaep",
        "aes-256-gcm",
    )
    if any(marker not in structure for marker in required_markers) or structure.count(":sha256") < 2:
        raise ArchiveError("OpenSSL produced a CMS archive with unexpected algorithms")


def _allocate_encrypted_temp(output_dir: Path, *, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        prefix=".encrypted-transcript-",
        suffix=suffix,
        dir=output_dir,
    )
    os.close(descriptor)
    return Path(raw_path)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some supported filesystems do not implement directory fsync.
        pass
    finally:
        os.close(descriptor)


def _prepare_output_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, exist_ok=True)
            mode = path.lstat().st_mode
        except OSError as exc:
            raise ArchiveError("Unable to create the archive output directory") from exc
    except OSError as exc:
        raise ArchiveError("Unable to inspect the archive output directory") from exc

    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ArchiveError("Archive output directory must be a real directory")


def _destination_file_exists(path: Path) -> bool:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ArchiveError("Unable to inspect an encrypted archive destination") from exc

    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        raise ArchiveError("Encrypted archive destination must be a regular file")
    if file_stat.st_size < 1:
        raise ArchiveError("Encrypted archive destination must not be empty")
    return True


def archive_transcript(
    transcript_path: Path,
    source_metadata_path: Path,
    recipient_cert: Path,
    output_dir: Path,
    *,
    openssl_binary: str = "openssl",
) -> ArchiveResult:
    """Encrypt one transcript into content-addressed JSON and Markdown CMS files."""

    transcript = _load_json_object(Path(transcript_path), label="transcript")
    source = _load_json_object(Path(source_metadata_path), label="source metadata")
    _validate_transcript(transcript)
    _validate_source(source)

    cert_path = Path(recipient_cert)
    if not cert_path.is_file():
        raise ArchiveError("Recipient certificate is unavailable")
    if not shutil.which(openssl_binary):
        raise ArchiveError("OpenSSL is unavailable")
    _validate_crypto_environment(cert_path, openssl_binary=openssl_binary)

    source_hash = json_sha256(source)
    transcript_hash = json_sha256(transcript)
    basename = f"{source_hash}__{transcript_hash}"
    destination = Path(output_dir)
    json_cms = destination / f"{basename}.json.cms"
    markdown_cms = destination / f"{basename}.md.cms"

    _prepare_output_directory(destination)
    json_exists = _destination_file_exists(json_cms)
    markdown_exists = _destination_file_exists(markdown_cms)
    if json_exists != markdown_exists:
        raise ArchiveError("Encrypted transcript archive pair is incomplete")
    if json_exists and markdown_exists:
        _validate_encrypted_cms(json_cms, openssl_binary=openssl_binary)
        _validate_encrypted_cms(markdown_cms, openssl_binary=openssl_binary)
        return ArchiveResult(
            json_cms=json_cms,
            markdown_cms=markdown_cms,
            source_sha256=source_hash,
            transcript_sha256=transcript_hash,
            created=False,
        )

    json_temp: Path | None = None
    markdown_temp: Path | None = None
    try:
        # The temporary directory is private (0700).  No plaintext file is ever
        # created below the persistent archive destination.
        with tempfile.TemporaryDirectory(prefix="bbg-transcript-plaintext-") as raw_temp_dir:
            plaintext_dir = Path(raw_temp_dir)
            json_plaintext = plaintext_dir / "transcript.json"
            markdown_plaintext = plaintext_dir / "transcript.md"
            json_document, markdown_document = build_plaintext_documents(
                source,
                transcript,
                source_sha256=source_hash,
                transcript_sha256=transcript_hash,
            )
            json_plaintext.write_bytes(json_document)
            markdown_plaintext.write_bytes(markdown_document)

            json_temp = _allocate_encrypted_temp(destination, suffix=".json.cms.tmp")
            markdown_temp = _allocate_encrypted_temp(destination, suffix=".md.cms.tmp")
            _encrypt_cms(
                json_plaintext,
                json_temp,
                recipient_cert=cert_path,
                openssl_binary=openssl_binary,
            )
            _encrypt_cms(
                markdown_plaintext,
                markdown_temp,
                recipient_cert=cert_path,
                openssl_binary=openssl_binary,
            )

        _validate_encrypted_cms(json_temp, openssl_binary=openssl_binary)
        _validate_encrypted_cms(markdown_temp, openssl_binary=openssl_binary)
        _fsync_file(json_temp)
        _fsync_file(markdown_temp)
        os.replace(json_temp, json_cms)
        json_temp = None
        os.replace(markdown_temp, markdown_cms)
        markdown_temp = None
        _fsync_directory(destination)
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveError("Unable to persist encrypted transcript archives") from exc
    finally:
        for temporary_path in (json_temp, markdown_temp):
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

    return ArchiveResult(
        json_cms=json_cms,
        markdown_cms=markdown_cms,
        source_sha256=source_hash,
        transcript_sha256=transcript_hash,
        created=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--source-metadata", required=True, type=Path)
    parser.add_argument("--recipient-cert", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--openssl", default="openssl", dest="openssl_binary")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        result = archive_transcript(
            args.transcript,
            args.source_metadata,
            args.recipient_cert,
            args.output_dir,
            openssl_binary=args.openssl_binary,
        )
    except ArchiveError as exc:
        raise SystemExit(f"Encrypted transcript archive failed: {exc}") from None

    action = "created" if result.created else "already exists"
    print(f"Encrypted transcript archive {action}: {result.json_cms.name}", flush=True)
    print(f"Encrypted transcript archive {action}: {result.markdown_cms.name}", flush=True)


if __name__ == "__main__":
    main()

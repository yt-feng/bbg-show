#!/usr/bin/env python3
"""Safely promote staged encrypted transcript pairs into one archive directory."""

from __future__ import annotations

import argparse
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path


ARCHIVE_DIRECTORY_NAME = "_transcript_archive"
ARCHIVE_NAME_RE = re.compile(
    r"^(?P<basename>[0-9a-f]{64}__[0-9a-f]{64})\.(?P<kind>json|md)\.cms$"
)


class PromotionError(RuntimeError):
    """Raised when staged archives cannot be promoted safely."""


@dataclass(frozen=True)
class PromotionResult:
    promoted_files: tuple[str, ...]
    existing_files: tuple[str, ...]
    staging_directories: int

    @property
    def promoted_count(self) -> int:
        return len(self.promoted_files)

    @property
    def existing_count(self) -> int:
        return len(self.existing_files)


@dataclass(frozen=True)
class StagedArchive:
    container: Path
    archive_directory: Path
    basename: str
    json_cms: Path
    markdown_cms: Path

    @property
    def files(self) -> tuple[Path, Path]:
        return self.json_cms, self.markdown_cms


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_within(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _reject_symlink_path(path: Path, *, label: str) -> None:
    """Reject an existing input path that is itself a symlink."""

    try:
        mode = _absolute(path).lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PromotionError(f"Unable to inspect {label}") from exc
    if stat.S_ISLNK(mode):
        raise PromotionError(f"{label.capitalize()} must not be a symlink")


def _existing_directory(path: Path, *, label: str) -> Path:
    _reject_symlink_path(path, label=label)
    absolute = _absolute(path)
    try:
        mode = absolute.lstat().st_mode
    except FileNotFoundError as exc:
        raise PromotionError(f"{label.capitalize()} is missing") from exc
    except OSError as exc:
        raise PromotionError(f"Unable to inspect {label}") from exc
    if not stat.S_ISDIR(mode):
        raise PromotionError(f"{label.capitalize()} must be a directory")
    try:
        return absolute.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"Unable to resolve {label}") from exc


def _validate_regular_nonempty(path: Path, *, label: str) -> None:
    try:
        mode_and_size = path.lstat()
    except OSError as exc:
        raise PromotionError(f"Unable to inspect {label}") from exc
    if stat.S_ISLNK(mode_and_size.st_mode):
        raise PromotionError(f"{label.capitalize()} must not be a symlink")
    if not stat.S_ISREG(mode_and_size.st_mode) or mode_and_size.st_size < 1:
        raise PromotionError(f"{label.capitalize()} must be a non-empty regular file")


def _scan_archive_directory(container: Path, archive_directory: Path) -> StagedArchive:
    try:
        archive_mode = archive_directory.lstat().st_mode
    except OSError as exc:
        raise PromotionError("Unable to inspect a transcript staging directory") from exc
    if stat.S_ISLNK(archive_mode):
        raise PromotionError("Transcript staging directory must not be a symlink")
    if not stat.S_ISDIR(archive_mode):
        raise PromotionError("Transcript staging path must be a directory")

    resolved_container = container.resolve(strict=True)
    resolved_archive = archive_directory.resolve(strict=True)
    if resolved_archive.parent != resolved_container:
        raise PromotionError("Transcript staging directory escapes its container")

    try:
        entries = sorted(os.scandir(resolved_archive), key=lambda entry: entry.name)
    except OSError as exc:
        raise PromotionError("Unable to read a transcript staging directory") from exc
    if len(entries) != 2:
        raise PromotionError("Transcript staging directory must contain exactly two files")

    basename: str | None = None
    by_kind: dict[str, Path] = {}
    for entry in entries:
        match = ARCHIVE_NAME_RE.fullmatch(entry.name)
        if match is None:
            raise PromotionError(f"Invalid encrypted transcript filename: {entry.name}")
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise PromotionError(f"Unable to inspect encrypted transcript: {entry.name}") from exc
        if entry.is_symlink():
            raise PromotionError(f"Encrypted transcript must not be a symlink: {entry.name}")
        if not stat.S_ISREG(entry_stat.st_mode) or entry_stat.st_size < 1:
            raise PromotionError(
                f"Encrypted transcript must be a non-empty regular file: {entry.name}"
            )

        current_basename = match.group("basename")
        kind = match.group("kind")
        if basename is None:
            basename = current_basename
        elif current_basename != basename:
            raise PromotionError("Encrypted transcript pair has mismatched basenames")
        if kind in by_kind:
            raise PromotionError("Encrypted transcript staging directory has a duplicate file kind")

        source = resolved_archive / entry.name
        try:
            resolved_source = source.resolve(strict=True)
        except OSError as exc:
            raise PromotionError(f"Unable to resolve encrypted transcript: {entry.name}") from exc
        if resolved_source.parent != resolved_archive or resolved_source.name != entry.name:
            raise PromotionError(f"Encrypted transcript escapes staging: {entry.name}")
        by_kind[kind] = resolved_source

    if basename is None or set(by_kind) != {"json", "md"}:
        raise PromotionError(
            "Transcript staging directory must contain one JSON and one Markdown file"
        )
    return StagedArchive(
        container=resolved_container,
        archive_directory=resolved_archive,
        basename=basename,
        json_cms=by_kind["json"],
        markdown_cms=by_kind["md"],
    )


def _scan_staging_root(staging_root: Path) -> tuple[StagedArchive, ...]:
    try:
        root_entries = sorted(os.scandir(staging_root), key=lambda entry: entry.name)
    except OSError as exc:
        raise PromotionError("Unable to read the staging root") from exc

    archives: list[StagedArchive] = []
    for entry in root_entries:
        if entry.is_symlink():
            raise PromotionError(f"Staging root must not contain symlinks: {entry.name}")
        try:
            is_directory = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            raise PromotionError(f"Unable to inspect staging entry: {entry.name}") from exc
        if not is_directory:
            continue

        container = staging_root / entry.name
        try:
            resolved_container = container.resolve(strict=True)
        except OSError as exc:
            raise PromotionError(f"Unable to resolve staging entry: {entry.name}") from exc
        if resolved_container.parent != staging_root:
            raise PromotionError(f"Staging entry escapes the staging root: {entry.name}")

        archive_directory = resolved_container / ARCHIVE_DIRECTORY_NAME
        try:
            archive_directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise PromotionError("Unable to inspect a transcript staging directory") from exc
        archives.append(_scan_archive_directory(resolved_container, archive_directory))
    return tuple(archives)


def _validate_destination(path: Path) -> None:
    _validate_regular_nonempty(path, label=f"archive destination {path.name}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PromotionError(f"Unable to resolve archive destination: {path.name}") from exc
    if resolved.parent != path.parent.resolve(strict=True) or resolved.name != path.name:
        raise PromotionError(f"Archive destination escapes the output directory: {path.name}")


def _destination_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PromotionError(f"Unable to inspect archive destination: {path.name}") from exc
    _validate_destination(path)
    return True


def _publish_without_clobber(source: Path, destination: Path) -> bool:
    """Atomically publish one file, preserving a destination that wins a race."""

    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        _validate_destination(destination)
        created = False
    except OSError as exc:
        raise PromotionError(f"Unable to publish encrypted transcript: {destination.name}") from exc
    else:
        created = True

    try:
        source.unlink()
    except OSError as exc:
        raise PromotionError(f"Unable to clean staged transcript: {source.name}") from exc
    return created


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def promote_encrypted_transcripts(
    staging_root: Path,
    output_dir: Path,
) -> PromotionResult:
    """Validate and promote every direct child's encrypted transcript staging pair."""

    raw_staging_root = Path(staging_root)
    try:
        raw_staging_root.lstat()
    except FileNotFoundError:
        return PromotionResult((), (), 0)
    except OSError as exc:
        raise PromotionError("Unable to inspect the staging root") from exc

    resolved_staging_root = _existing_directory(raw_staging_root, label="staging root")
    raw_output_dir = Path(output_dir)
    _reject_symlink_path(raw_output_dir, label="output directory")
    resolved_output_dir = _absolute(raw_output_dir).resolve(strict=False)
    if _is_within(resolved_output_dir, resolved_staging_root) or _is_within(
        resolved_staging_root, resolved_output_dir
    ):
        raise PromotionError("Staging root and output directory must not overlap")

    archives = _scan_staging_root(resolved_staging_root)
    if not archives:
        return PromotionResult((), (), 0)

    # Validate all existing targets before mutating any staging directory.
    for archive in archives:
        for source in archive.files:
            destination = resolved_output_dir / source.name
            if not _is_within(destination, resolved_output_dir):
                raise PromotionError(f"Archive destination escapes output: {source.name}")
            _destination_exists(destination)

    try:
        resolved_output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PromotionError("Unable to create the transcript output directory") from exc
    _reject_symlink_path(resolved_output_dir, label="output directory")
    try:
        actual_output_dir = resolved_output_dir.resolve(strict=True)
    except OSError as exc:
        raise PromotionError("Unable to resolve the transcript output directory") from exc
    if actual_output_dir != resolved_output_dir or not actual_output_dir.is_dir():
        raise PromotionError("Output directory changed while promoting transcripts")

    promoted: list[str] = []
    existing: list[str] = []
    for archive in archives:
        for source in archive.files:
            destination = actual_output_dir / source.name
            if _publish_without_clobber(source, destination):
                promoted.append(destination.name)
            else:
                existing.append(destination.name)
        try:
            archive.archive_directory.rmdir()
        except OSError as exc:
            raise PromotionError(
                "Unable to remove an emptied transcript staging directory"
            ) from exc
        try:
            archive.container.rmdir()
        except OSError:
            # Top Video containers normally retain rendered outputs.  Remove the
            # container only when the encrypted pair was its final content.
            pass

    _fsync_directory(actual_output_dir)
    _fsync_directory(resolved_staging_root)
    return PromotionResult(
        tuple(sorted(promoted)),
        tuple(sorted(existing)),
        len(archives),
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        result = promote_encrypted_transcripts(args.staging_root, args.output_dir)
    except PromotionError as exc:
        raise SystemExit(f"Encrypted transcript promotion failed: {exc}") from None

    for filename in result.promoted_files:
        print(f"Promoted encrypted transcript: {filename}", flush=True)
    for filename in result.existing_files:
        print(f"Kept existing encrypted transcript: {filename}", flush=True)
    print(
        "Encrypted transcript promotion complete: "
        f"staging={result.staging_directories} "
        f"promoted={result.promoted_count} existing={result.existing_count}",
        flush=True,
    )


if __name__ == "__main__":
    main()

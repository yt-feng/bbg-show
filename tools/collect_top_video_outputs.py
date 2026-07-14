#!/usr/bin/env python3
"""Validate Top Video artifacts and preserve prior output on all-skipped reruns."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any

from content_fingerprint import validate_fingerprint
from top_video_sources import (
    backfill_processed_sources_from_git_history,
    find_duplicate_content,
    item_source_key,
    load_ledger,
    update_processed_sources,
)


class BatchEvaluationError(RuntimeError):
    """Raised when a batch had processable candidates but none succeeded."""


SUMMARY_NAME_RE = re.compile(r"^summary_(\d{2})\.json$")
VALID_RESULT_STATUSES = {"success", "failed", "skipped"}


def summary_counts(videos: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(videos),
        "succeeded": sum(1 for item in videos if item.get("status") == "success"),
        "failed": sum(1 for item in videos if item.get("status") == "failed"),
        "skipped": sum(1 for item in videos if item.get("status") == "skipped"),
    }


def validate_success_output(
    output_dir: Path,
    item: dict[str, Any],
    *,
    expected_index: int,
) -> tuple[str, str]:
    label = Path(str(item.get("output_dir", ""))).name
    if not label or label in {".", ".."}:
        return "", "success result has no valid output directory"
    expected_prefix = f"{expected_index:02d}_"
    if not label.startswith(expected_prefix):
        return label, (
            f"declared output directory {label!r} does not match "
            f"summary index {expected_index:02d}"
        )

    rendered_files = item.get("rendered_files")
    if not isinstance(rendered_files, list) or not rendered_files:
        return label, "success result declares no rendered files"

    base = output_dir / label
    if not base.is_dir():
        return label, f"declared output directory is missing: {label}"

    declared: set[Path] = set()
    for value in rendered_files:
        name = str(value)
        relative = Path(name)
        if relative.name != name or relative.suffix.lower() != ".mp4":
            return label, f"invalid rendered filename: {name!r}"
        path = base / relative
        if not path.is_file() or path.stat().st_size < 1:
            return label, f"declared rendered file is missing or empty: {name}"
        declared.add(path.resolve())

    for path in base.rglob("*.mp4"):
        if path.resolve() not in declared:
            path.unlink()
            print(f"Removed undeclared MP4: {path}", flush=True)
    return label, ""


def summary_index(summary_path: Path) -> int | None:
    match = SUMMARY_NAME_RE.fullmatch(summary_path.name)
    if not match:
        return None
    index = int(match.group(1))
    return index if index >= 1 else None


def failed_item(index: int, error: str, original: dict[str, Any] | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {}
    if original:
        for key in ("url", "title", "source", "youtube_id"):
            if key in original:
                item[key] = original[key]
    item.update({"status": "failed", "index": index, "error": error})
    return item


def failed_summary(
    index: int,
    error: str,
    payload: dict[str, Any] | None = None,
    original: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = dict(payload) if payload is not None else {}
    normalized["video_index"] = index
    normalized["videos"] = [failed_item(index, error, original)]
    normalized.update(summary_counts(normalized["videos"]))
    return normalized


def valid_item_index(item: dict[str, Any], expected_index: int) -> bool:
    value = item.get("index")
    return isinstance(value, int) and not isinstance(value, bool) and value == expected_index


def prune_unsuccessful_outputs(
    output_dir: Path,
    *,
    expected_total: int | None = None,
) -> dict[str, int]:
    if expected_total is not None and expected_total < 0:
        raise ValueError("expected_total cannot be negative")
    allowed_dirs: set[str] = set()
    changed_summaries = 0
    invalid_successes = 0
    invalid_summaries = 0
    invalid_items = 0

    for summary_path in sorted(output_dir.glob("summary_*.json")):
        index = summary_index(summary_path)
        if index is None or (expected_total is not None and index > expected_total):
            summary_path.unlink()
            invalid_summaries += 1
            reason = (
                "invalid filename"
                if index is None
                else f"index exceeds expected total {expected_total}"
            )
            print(f"Removed summary with {reason}: {summary_path.name}", flush=True)
            continue

        changed = False
        try:
            raw_payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            error = f"invalid process summary: {exc}"
            payload = failed_summary(index, error)
            invalid_summaries += 1
            changed = True
            print(f"Replaced invalid {summary_path.name}: {error}", flush=True)
        else:
            if not isinstance(raw_payload, dict):
                error = "invalid process summary: root must be an object"
                payload = failed_summary(index, error)
                invalid_summaries += 1
                changed = True
                print(f"Replaced invalid {summary_path.name}: {error}", flush=True)
            else:
                payload = raw_payload
                videos = payload.get("videos")
                if not isinstance(videos, list):
                    error = "invalid process summary: videos must be a list"
                    payload = failed_summary(index, error, payload)
                    invalid_summaries += 1
                    changed = True
                    print(f"Replaced invalid {summary_path.name}: {error}", flush=True)
                else:
                    dict_items = [item for item in videos if isinstance(item, dict)]
                    non_dict_count = len(videos) - len(dict_items)
                    if non_dict_count:
                        invalid_items += non_dict_count
                        changed = True
                        print(
                            f"Ignored {non_dict_count} non-object result(s) in {summary_path.name}",
                            flush=True,
                        )

                    if len(dict_items) != 1:
                        error = (
                            "invalid process summary: expected exactly one object result "
                            f"for index {index:02d}"
                        )
                        payload = failed_summary(index, error, payload)
                        invalid_summaries += 1
                        invalid_items += len(dict_items)
                        changed = True
                        print(f"Replaced invalid {summary_path.name}: {error}", flush=True)
                    else:
                        item = dict_items[0]
                        payload["videos"] = [item]
                        status = item.get("status")
                        if not valid_item_index(item, index):
                            error = (
                                f"result index {item.get('index')!r} does not match "
                                f"summary index {index:02d}"
                            )
                            if status == "success":
                                invalid_successes += 1
                            invalid_items += 1
                            payload = failed_summary(index, error, payload, item)
                            changed = True
                            print(f"Replaced invalid result in {summary_path.name}: {error}", flush=True)
                        elif status not in VALID_RESULT_STATUSES:
                            error = f"invalid result status for index {index:02d}: {status!r}"
                            invalid_items += 1
                            payload = failed_summary(index, error, payload, item)
                            changed = True
                            print(f"Replaced invalid result in {summary_path.name}: {error}", flush=True)
                        elif status == "success":
                            label, error = validate_success_output(
                                output_dir,
                                item,
                                expected_index=index,
                            )
                            if error:
                                item["status"] = "failed"
                                item["error"] = error
                                invalid_successes += 1
                                changed = True
                                print(
                                    f"Invalid successful output in {summary_path.name}: {error}",
                                    flush=True,
                                )
                            else:
                                allowed_dirs.add(label)

        videos = payload["videos"]
        counts = summary_counts(videos)
        if any(payload.get(key) != value for key, value in counts.items()):
            payload.update(counts)
            changed = True

        if changed:
            summary_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            changed_summaries += 1

    removed_dirs = 0
    for child in sorted(output_dir.iterdir()):
        if child.is_dir() and child.name not in allowed_dirs:
            shutil.rmtree(child)
            removed_dirs += 1
            print(f"Removed unsuccessful output directory: {child}", flush=True)

    result = {
        "allowed_dirs": len(allowed_dirs),
        "removed_dirs": removed_dirs,
        "invalid_successes": invalid_successes,
        "invalid_summaries": invalid_summaries,
        "invalid_items": invalid_items,
        "changed_summaries": changed_summaries,
    }
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def _validated_success_fingerprints(
    item: dict[str, Any],
) -> tuple[dict[str, Any], float, list[dict[str, Any]]]:
    """Validate the fingerprints that every newly rendered success must carry."""
    raw_source = item.get("source_fingerprint")
    if raw_source in (None, ""):
        raise ValueError("successful output has no source_fingerprint")
    source_fingerprint = validate_fingerprint(raw_source)
    try:
        source_duration = float(item.get("source_duration", item.get("duration", 0)))
    except (TypeError, ValueError) as exc:
        raise ValueError("successful output has an invalid source_duration") from exc
    if source_duration <= 0:
        raise ValueError("successful output has no positive source_duration")

    raw_clips = item.get("clip_fingerprints")
    if not isinstance(raw_clips, list) or not raw_clips:
        raise ValueError("successful output has no clip_fingerprints")
    clips: list[dict[str, Any]] = []
    for index, raw_clip in enumerate(raw_clips, start=1):
        details = dict(raw_clip) if isinstance(raw_clip, dict) else {}
        raw_fingerprint = details.get("clip_fingerprint", raw_clip)
        details["clip_fingerprint"] = validate_fingerprint(raw_fingerprint)
        details.setdefault("clip_index", index)
        clips.append(details)
    return source_fingerprint, source_duration, clips


def _append_batch_identity(
    ledger: dict[str, Any],
    item: dict[str, Any],
    *,
    run_date: str,
    source_fingerprint: dict[str, Any],
    source_duration: float,
    clip_fingerprints: list[dict[str, Any]],
) -> None:
    """Make one accepted index visible to later indexes in the same matrix batch."""
    key = item_source_key(item) or f"batch-index:{item.get('index', '')}"
    source_record = {
        "source_key": key,
        "url": str(item.get("url", "")),
        "source_title": str(item.get("source_title", item.get("title", ""))),
        "title": str(item.get("title", "")),
        "processed_on": run_date,
        "source_fingerprint": source_fingerprint,
        "source_duration": source_duration,
    }
    ledger["sources"].append(source_record)
    for raw_clip in clip_fingerprints:
        clip = dict(raw_clip)
        clip.update({
            "source_key": key,
            "url": str(item.get("url", "")),
            "source_title": str(item.get("source_title", item.get("title", ""))),
            "processed_on": run_date,
        })
        ledger["clips"].append(clip)


def deduplicate_success_outputs(output_dir: Path, ledger_path: Path) -> dict[str, int]:
    """Reject historical or same-batch content repeats after artifact validation."""
    historical = load_ledger(ledger_path)
    current_batch: dict[str, Any] = {
        "version": 2,
        "sources": [],
        "clips": [],
        "history_backfilled": False,
    }
    accepted = 0
    duplicates = 0
    invalid_fingerprints = 0

    for summary_path in sorted(output_dir.glob("summary_*.json")):
        index = summary_index(summary_path)
        if index is None:
            continue
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
        videos = payload.get("videos")
        if not isinstance(videos, list) or len(videos) != 1 or not isinstance(videos[0], dict):
            continue
        item = videos[0]
        if item.get("status") != "success":
            continue

        try:
            source_fingerprint, source_duration, clip_fingerprints = (
                _validated_success_fingerprints(item)
            )
        except (TypeError, ValueError) as exc:
            item["status"] = "failed"
            item["error"] = f"content fingerprint validation failed: {exc}"
            label = Path(str(item.get("output_dir", ""))).name
            if label and (output_dir / label).is_dir():
                shutil.rmtree(output_dir / label)
            invalid_fingerprints += 1
            print(f"Rejected Top Video {index:02d}: {item['error']}", flush=True)
        else:
            duplicate = find_duplicate_content(
                historical,
                source_fingerprint=source_fingerprint,
                source_duration=source_duration,
                clip_fingerprints=clip_fingerprints,
            )
            if duplicate is None:
                duplicate = find_duplicate_content(
                    current_batch,
                    source_fingerprint=source_fingerprint,
                    source_duration=source_duration,
                    clip_fingerprints=clip_fingerprints,
                )
            if duplicate is not None:
                item["status"] = "skipped"
                item["skip_reason"] = "duplicate_content"
                item["duplicate_stage"] = "collector"
                item["duplicate_kind"] = str(duplicate.get("duplicate_kind", "content"))
                item["duplicate_of"] = str(
                    duplicate.get("duplicate_of", "previously accepted Top Video")
                )
                item["error"] = "Top video skipped because its content was already published"
                item["rendered_files"] = []
                label = Path(str(item.get("output_dir", ""))).name
                if label and (output_dir / label).is_dir():
                    shutil.rmtree(output_dir / label)
                duplicates += 1
                print(
                    f"Skipped duplicate Top Video {index:02d}: {item['duplicate_of']}",
                    flush=True,
                )
            else:
                _append_batch_identity(
                    current_batch,
                    item,
                    run_date=str(payload.get("run_date", "")),
                    source_fingerprint=source_fingerprint,
                    source_duration=source_duration,
                    clip_fingerprints=clip_fingerprints,
                )
                accepted += 1

        payload.update(summary_counts(payload["videos"]))
        summary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    result = {
        "accepted": accepted,
        "duplicates": duplicates,
        "invalid_fingerprints": invalid_fingerprints,
    }
    print(json.dumps(result, sort_keys=True), flush=True)
    return result


def evaluate_batch_summary(summary: dict[str, Any]) -> dict[str, Any]:
    total = int(summary.get("total") or 0)
    succeeded = int(summary.get("succeeded") or 0)
    failed = int(summary.get("failed") or 0)
    skipped = int(summary.get("skipped") or 0)
    processable = max(total - skipped, 0)
    failure_rate = (failed / processable) if processable else 0.0
    result = {
        "total": total,
        "succeeded": succeeded,
        "failed": failed,
        "skipped": skipped,
        "processable": processable,
        "failure_rate": failure_rate,
    }
    print(
        f"Top video batch: total={total}, succeeded={succeeded}, "
        f"failed={failed}, skipped={skipped}, failure_rate={failure_rate:.1%}",
        flush=True,
    )
    if processable < 1:
        print("All top videos were skipped by content filters; keeping workflow green.", flush=True)
        result["outcome"] = "no_processable_candidates"
        return result
    if succeeded < 1:
        raise BatchEvaluationError("No top videos were processed successfully.")
    if failed:
        print(
            f"::warning::{failed}/{total} top videos failed "
            f"({failure_rate:.1%}); at least one video succeeded, so successful clips will be committed.",
            flush=True,
        )
        result["outcome"] = "partial_success"
    else:
        result["outcome"] = "success"
    return result


def evaluate_batch_file(output_dir: Path) -> dict[str, Any]:
    summary_path = output_dir / "summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"Current summary does not exist: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    try:
        return evaluate_batch_summary(summary)
    except BatchEvaluationError as exc:
        raise SystemExit(str(exc)) from exc


def restore_previous_if_no_success(output_dir: Path, previous_dir: Path) -> bool:
    summary_path = output_dir / "summary.json"
    if not summary_path.is_file():
        raise SystemExit(f"Current summary does not exist: {summary_path}")
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    total = int(summary.get("total") or 0)
    succeeded = int(summary.get("succeeded") or 0)
    skipped = int(summary.get("skipped") or 0)
    no_successful_candidates = succeeded == 0 and (total == 0 or skipped == total)
    previous_has_content = previous_dir.is_dir() and (
        (previous_dir / "summary.json").is_file()
        or any(previous_dir.rglob("*.mp4"))
    )
    if not no_successful_candidates or not previous_has_content:
        return False

    shutil.rmtree(output_dir)
    shutil.copytree(previous_dir, output_dir)
    print("Restored the previously published output; this all-skipped rerun is a no-op.", flush=True)
    return True


def record_successful_source_history(
    ledger_path: Path,
    history_root: Path,
    output_dir: Path,
) -> tuple[int, int]:
    """Backfill retained successes, then include the explicitly collected output."""
    summary_paths = set(history_root.glob("*/summary.json")) if history_root.is_dir() else set()
    current_summary = output_dir / "summary.json"
    if current_summary.is_file():
        summary_paths.add(current_summary)
    recorded = 0
    for summary_path in sorted(summary_paths):
        recorded += update_processed_sources(ledger_path, summary_path)
    return recorded, len(summary_paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--expected-total",
        type=int,
        default=None,
        help="Reject per-video summaries whose index exceeds this manifest count.",
    )
    parser.add_argument(
        "--restore-previous",
        type=Path,
        default=None,
        help="Restore this previous output directory when the current summary has no successful candidates.",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Fail only when the summary has processable candidates but zero successful videos.",
    )
    parser.add_argument(
        "--record-successes",
        action="store_true",
        help="Add successful items from the final summary to the persistent source ledger.",
    )
    parser.add_argument(
        "--backfill-git-history",
        action="store_true",
        help="Import all historical Top Videos highlight plans into the content ledger once.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Git repository root used by --backfill-git-history.",
    )
    parser.add_argument(
        "--deduplicate-content",
        action="store_true",
        help="After artifact validation, skip historical and same-batch content duplicates.",
    )
    parser.add_argument(
        "--processed-sources",
        type=Path,
        default=Path("rendered-clips/top-videos/processed_sources.json"),
        help="Persistent successful-source ledger.",
    )
    parser.add_argument(
        "--history-root",
        type=Path,
        default=Path("rendered-clips/top-videos"),
        help="Top Videos root whose retained successful summaries should backfill the ledger.",
    )
    args = parser.parse_args()
    if not args.output_dir.is_dir():
        raise SystemExit(f"Output directory does not exist: {args.output_dir}")
    selected_modes = sum(
        (
            args.restore_previous is not None,
            args.evaluate,
            args.record_successes,
            args.backfill_git_history,
        )
    )
    if selected_modes > 1:
        raise SystemExit(
            "--restore-previous, --evaluate, --record-successes, and "
            "--backfill-git-history cannot be used together"
        )
    if args.deduplicate_content and selected_modes:
        raise SystemExit("--deduplicate-content can only be used with artifact pruning")
    if args.evaluate:
        evaluate_batch_file(args.output_dir)
    elif args.record_successes:
        try:
            recorded, summary_count = record_successful_source_history(
                args.processed_sources,
                args.history_root,
                args.output_dir,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"Recorded {recorded} successful Top Videos source result(s) from "
            f"{summary_count} summary file(s) in {args.processed_sources}",
            flush=True,
        )
    elif args.backfill_git_history:
        try:
            imported = backfill_processed_sources_from_git_history(
                args.processed_sources,
                args.repo_root,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            f"Imported {imported} historical Top Videos plan(s) into "
            f"{args.processed_sources}",
            flush=True,
        )
    elif args.restore_previous is not None:
        restore_previous_if_no_success(args.output_dir, args.restore_previous)
    else:
        prune_unsuccessful_outputs(args.output_dir, expected_total=args.expected_total)
        if args.deduplicate_content:
            try:
                deduplicate_success_outputs(args.output_dir, args.processed_sources)
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()

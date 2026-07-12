#!/usr/bin/env python3
"""Validate Top Video artifacts and preserve prior output on all-skipped reruns."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from pathlib import Path
from typing import Any


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
    args = parser.parse_args()
    if not args.output_dir.is_dir():
        raise SystemExit(f"Output directory does not exist: {args.output_dir}")
    if args.restore_previous is not None and args.evaluate:
        raise SystemExit("--restore-previous and --evaluate cannot be used together")
    if args.evaluate:
        evaluate_batch_file(args.output_dir)
    elif args.restore_previous is not None:
        restore_previous_if_no_success(args.output_dir, args.restore_previous)
    else:
        prune_unsuccessful_outputs(args.output_dir, expected_total=args.expected_total)


if __name__ == "__main__":
    main()

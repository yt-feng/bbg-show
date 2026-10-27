#!/usr/bin/env python3
"""Retry failed show speakers while retaining already published clips."""

from __future__ import annotations

import argparse
import copy
import json
from datetime import date
from pathlib import Path

from render_clips_linux import output_name


def read_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read show repair metadata {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Show repair metadata must be an object: {path}")
    return payload


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def failed_names(plan: dict) -> set[str]:
    return {
        item["speaker"] for item in plan.get("skipped_speakers", [])
        if str(item.get("reason", "")).startswith("planner failed")
    }


def prepare(output: Path, work: Path, show_date: str) -> None:
    try:
        if date.fromisoformat(show_date).isoformat() != show_date:
            raise ValueError(show_date)
    except ValueError as exc:
        raise SystemExit("repair_failed_speakers requires an explicit YYYY-MM-DD show_date") from exc
    previous = read_json(output / "highlight_plan.json")
    selected = read_json(output / "speakers.json")
    identity = read_json(output / "source_identity.json")
    if identity.get("show_date") != show_date or not identity.get("asset_id"):
        raise SystemExit("Saved source identity does not match the requested repair date")
    failed = failed_names(previous)
    if not previous.get("clips") or not failed:
        raise SystemExit("No published partial show with failed speakers is available to repair")
    retry = [item for item in selected["speakers"] if item["speaker"] in failed]
    if {item["speaker"] for item in retry} != failed:
        raise SystemExit("Saved speaker metadata does not cover every failed speaker")
    for index, clip in enumerate(previous["clips"], start=1):
        video = output / output_name(index, clip)
        if not video.is_file() or video.stat().st_size == 0:
            raise SystemExit(f"Published clip is missing or empty: {video}")
    write_json(work / "previous_plan.json", previous)
    write_json(work / "previous_speakers.json", selected)
    write_json(work / "previous_identity.json", identity)
    retry_selection = {**selected, "speakers": retry, "selection_status": "selected"}
    write_json(work / "repair_speakers.json", retry_selection)
    print("Repairing failed speakers: " + ", ".join(sorted(failed)))


def verify_source(work: Path) -> None:
    previous = read_json(work / "previous_identity.json")
    current = read_json(work / "source_identity.json")
    if any(previous.get(key) != current.get(key) for key in ("show_date", "asset_id")):
        raise SystemExit("Repair downloaded a different source from the previously published clips")


def merge(work: Path) -> None:
    from plan_daily_highlights import deduplicate_plan_clips

    previous = read_json(work / "previous_plan.json")
    repaired = read_json(work / "highlight_plan.json")
    original_speakers = read_json(work / "previous_speakers.json")
    retry = read_json(work / "repair_speakers.json")
    expected_status = "planned" if repaired.get("clips") else "no_eligible_clips"
    if repaired.get("planning_status") != expected_status or failed_names(repaired):
        raise SystemExit("Cannot merge a failed or incomplete speaker repair")
    indexes = {item["speaker"]: index for index, item in enumerate(original_speakers["speakers"], start=1)}
    retried_names = {item["speaker"] for item in retry["speakers"]}
    for clip in repaired["clips"]:
        retry_index = clip.get("daily_speaker_index")
        if not isinstance(retry_index, int) or not 1 <= retry_index <= len(retry["speakers"]):
            raise SystemExit("Repair plan contains an invalid failed-speaker index")
        # The planner may localize a display name; its selection index is stable.
        original_name = retry["speakers"][retry_index - 1]["speaker"]
        clip["daily_speaker_index"] = indexes[original_name]
    merged = copy.deepcopy(previous)
    retained_count = len(previous["clips"])
    merged["clips"].extend(repaired["clips"])
    merged["skipped_speakers"] = [
        item for item in previous.get("skipped_speakers", [])
        if not (item.get("speaker") in retried_names and str(item.get("reason", "")).startswith("planner failed"))
    ] + repaired.get("skipped_speakers", [])
    merged["plan_files"] = previous.get("plan_files", []) + repaired.get("plan_files", [])
    merged["planning_status"] = "planned"
    merged.setdefault("repairs", []).append({
        "retained_clip_count": retained_count,
        "retried_speakers": sorted(retried_names),
        "planning_status": repaired["planning_status"],
    })
    deduplicate_plan_clips(merged, stage="repair_merge")
    if merged["clips"][:retained_count] != previous["clips"]:
        raise SystemExit("Repair changed existing clip order or content")
    write_json(work / "highlight_plan.json", merged)
    write_json(work / "speakers.json", original_speakers)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["prepare", "verify-source", "merge"])
    parser.add_argument("--work-dir", type=Path, default=Path("work/daily"))
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--show-date", default="")
    args = parser.parse_args()
    if args.command == "prepare":
        if args.output_dir is None:
            parser.error("prepare requires --output-dir")
        prepare(args.output_dir, args.work_dir, args.show_date)
    elif args.command == "verify-source":
        verify_source(args.work_dir)
    else:
        merge(args.work_dir)


if __name__ == "__main__":
    main()

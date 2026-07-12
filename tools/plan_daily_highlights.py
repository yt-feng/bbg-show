#!/usr/bin/env python3
"""Plan highlight clips for selected daily China Show speakers."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from plan_speaker_highlights import NO_ELIGIBLE_CLIPS_EXIT_CODE
from trump_filter import remove_trump_clips_from_plan


NO_ELIGIBLE_CLIP_MARKERS = (
    "no non-sensitive-topic clips remained",
    "no non-sensitive-topic clips found",
    "no non-sensitive-topic clips remained after filtering",
)


def slugify(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "speaker"


def is_no_eligible_clip_output(output: str) -> bool:
    lowered = output.casefold()
    return any(marker in lowered for marker in NO_ELIGIBLE_CLIP_MARKERS)


def run_and_stream(command: list[str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
    return proc


def write_combined_plan(
    path: Path,
    *,
    speakers_data: dict[str, Any],
    transcript: Path,
    speakers_path: Path,
    plan_files: list[str],
    skipped: list[dict[str, str]],
    clips: list[dict[str, Any]],
    planning_status: str,
) -> dict[str, Any]:
    combined = {
        "show_date": speakers_data.get("show_date", ""),
        "source_transcript": str(transcript),
        "source_speakers": str(speakers_path),
        "planning_status": planning_status,
        "plan_files": plan_files,
        "skipped_speakers": skipped,
        "clips": clips,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return combined


def refine_titles_or_restore_planner_plan(plan_path: Path, refiner: Path) -> str:
    """Refine titles, preserving a usable planner plan on non-content failures."""
    original_plan = plan_path.read_text(encoding="utf-8")
    proc = run_and_stream([sys.executable, str(refiner), "--plan", str(plan_path)])
    if proc.returncode == 0:
        return "refined"

    output = proc.stdout or ""
    if is_no_eligible_clip_output(output):
        return "no_eligible_clips"

    plan_path.write_text(original_plan, encoding="utf-8")
    print(
        f"::warning::Title refinement failed with exit code {proc.returncode}; "
        "using planner-generated titles after content filtering.",
        flush=True,
    )
    return "planner_fallback"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transcript", type=Path, required=True)
    parser.add_argument("--speakers", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--combined-plan", type=Path, required=True)
    parser.add_argument("--min-seconds", type=int, default=30)
    parser.add_argument("--max-seconds", type=int, default=150)
    parser.add_argument("--min-clips", type=int, default=3)
    parser.add_argument("--max-clips", type=int, default=5)
    parser.add_argument("--skip-title-refine", action="store_true")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Write an empty successful plan when every candidate is unsuitable for clipping.",
    )
    args = parser.parse_args()

    speakers_data = json.loads(args.speakers.read_text(encoding="utf-8"))
    speakers = speakers_data.get("speakers", [])
    if not speakers:
        raise SystemExit("No selected speakers to plan")

    planner = Path(__file__).with_name("plan_speaker_highlights.py")
    args.out_dir.mkdir(parents=True, exist_ok=True)

    combined_clips: list[dict[str, Any]] = []
    plan_files: list[str] = []
    skipped: list[dict[str, str]] = []
    planner_failures: list[dict[str, str]] = []

    for idx, speaker in enumerate(speakers, start=1):
        name = str(speaker["speaker"])
        context = str(speaker.get("speaker_context", ""))
        start = float(speaker["segment_start"])
        end = float(speaker["segment_end"])
        plan_path = args.out_dir / f"{idx:02d}_{slugify(name)}.json"
        print(f"Planning {name}: {start:.1f}-{end:.1f}", flush=True)
        command = [
            sys.executable,
            str(planner),
            "--transcript", str(args.transcript),
            "--speaker", name,
            "--speaker-context", context,
            "--segment-start", f"{start:.2f}",
            "--segment-end", f"{end:.2f}",
            "--min-seconds", str(args.min_seconds),
            "--max-seconds", str(args.max_seconds),
            "--min-clips", str(args.min_clips),
            "--max-clips", str(args.max_clips),
            "--out", str(plan_path),
            "--force",
        ]
        proc = run_and_stream(command)
        if proc.returncode:
            reason = f"planner failed with exit code {proc.returncode}"
            if proc.returncode == NO_ELIGIBLE_CLIPS_EXIT_CODE:
                skipped.append({"speaker": name, "reason": "no eligible clips after content quality filters"})
            else:
                planner_failures.append({"speaker": name, "reason": reason})
            continue

        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        removed = remove_trump_clips_from_plan(plan, use_ai=True)
        if removed:
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            skipped.append({"speaker": name, "reason": f"removed {len(removed)} sensitive-topic clip(s)"})
        clips = plan.get("clips", [])
        if not clips:
            skipped.append({"speaker": name, "reason": "planner returned no clips"})
            continue
        for clip in clips[: args.max_clips]:
            clip = dict(clip)
            clip["speaker"] = clip.get("speaker") or name
            clip["speaker_context"] = context
            clip["daily_speaker_index"] = idx
            combined_clips.append(clip)
        plan_files.append(str(plan_path))

    if not combined_clips:
        planning_status = "planner_failed" if planner_failures else "no_eligible_clips"
        write_combined_plan(
            args.combined_plan,
            speakers_data=speakers_data,
            transcript=args.transcript,
            speakers_path=args.speakers,
            plan_files=plan_files,
            skipped=[*skipped, *planner_failures],
            clips=[],
            planning_status=planning_status,
        )
        if planner_failures:
            names = ", ".join(item["speaker"] for item in planner_failures)
            raise SystemExit(f"Speaker planner failed for: {names}")
        print(f"No eligible clips for any speaker: {args.combined_plan}", flush=True)
        if args.allow_empty:
            return
        raise SystemExit("No clips planned for any speaker")

    skipped.extend(planner_failures)
    combined = write_combined_plan(
        args.combined_plan,
        speakers_data=speakers_data,
        transcript=args.transcript,
        speakers_path=args.speakers,
        plan_files=plan_files,
        skipped=skipped,
        clips=combined_clips,
        planning_status="planned",
    )
    title_refinement_status = "skipped"
    if not args.skip_title_refine:
        refiner = Path(__file__).with_name("refine_clip_titles.py")
        print("Refining combined clip titles with DeepSeek", flush=True)
        title_refinement_status = refine_titles_or_restore_planner_plan(
            args.combined_plan,
            refiner,
        )
        if title_refinement_status == "no_eligible_clips":
            combined = json.loads(args.combined_plan.read_text(encoding="utf-8"))
            combined["planning_status"] = "no_eligible_clips"
            combined["title_refinement_status"] = "no_eligible_clips"
            combined["clips"] = []
            args.combined_plan.write_text(
                json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print("No eligible clips remained after title refinement", flush=True)
            if args.allow_empty:
                return
            raise SystemExit("No non-sensitive-topic clips remained after title refinement")
        combined = json.loads(args.combined_plan.read_text(encoding="utf-8"))
        removed = remove_trump_clips_from_plan(combined, use_ai=True)
        if removed:
            print(f"Removed {len(removed)} sensitive-topic clip(s) after title refinement", flush=True)
            if not combined.get("clips"):
                combined["planning_status"] = "no_eligible_clips"
                args.combined_plan.write_text(
                    json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if args.allow_empty:
                    print("No eligible clips remained after title refinement", flush=True)
                    return
                raise SystemExit("No non-sensitive-topic clips remained after title refinement")
        combined["title_refinement_status"] = title_refinement_status
        args.combined_plan.write_text(
            json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    final_combined = json.loads(args.combined_plan.read_text(encoding="utf-8"))
    final_count = len(final_combined.get("clips", [])) if isinstance(final_combined.get("clips", []), list) else 0
    final_combined["planning_status"] = "planned" if final_count else "no_eligible_clips"
    final_combined.setdefault("title_refinement_status", title_refinement_status)
    args.combined_plan.write_text(
        json.dumps(final_combined, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote combined plan: {args.combined_plan} ({final_count} clips)", flush=True)


if __name__ == "__main__":
    main()

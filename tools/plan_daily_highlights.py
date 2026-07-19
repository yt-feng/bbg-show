#!/usr/bin/env python3
"""Plan highlight clips for selected daily China Show speakers."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from plan_speaker_highlights import NO_ELIGIBLE_CLIPS_EXIT_CODE
from title_refinement_status import read_title_refinement_status
from trump_filter import remove_trump_clips_from_plan


NO_ELIGIBLE_CLIP_MARKERS = (
    "no non-sensitive-topic clips remained",
    "no non-sensitive-topic clips found",
    "no non-sensitive-topic clips remained after filtering",
)

ENGLISH_WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?", re.IGNORECASE)
TIME_CONTAINMENT_THRESHOLD = 0.80
MIN_TIME_DUPLICATE_DURATION_RATIO = 0.70
MIN_TRANSCRIPT_DUPLICATE_WORDS = 45
FIVE_GRAM_CONTAINMENT_THRESHOLD = 0.86


def slugify(value: str) -> str:
    value = re.sub(r"\s+", "_", value.strip())
    value = re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "speaker"


def is_no_eligible_clip_output(output: str) -> bool:
    lowered = output.casefold()
    return any(marker in lowered for marker in NO_ELIGIBLE_CLIP_MARKERS)


def clip_english_words(clip: dict[str, Any]) -> list[str]:
    """Return normalized English subtitle words for duplicate comparison."""
    subtitles = clip.get("subtitles")
    if not isinstance(subtitles, list):
        return []
    text = " ".join(
        str(subtitle.get("en") or "")
        for subtitle in subtitles
        if isinstance(subtitle, dict)
    )
    return [match.group(0).lower() for match in ENGLISH_WORD_RE.finditer(text)]


def clip_time_containment(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Measure how much of the shorter clip is covered by the other clip."""
    try:
        left_start = float(left["start"])
        left_end = float(left["end"])
        right_start = float(right["start"])
        right_end = float(right["end"])
    except (KeyError, TypeError, ValueError):
        return 0.0
    values = (left_start, left_end, right_start, right_end)
    if not all(math.isfinite(value) for value in values):
        return 0.0
    left_duration = left_end - left_start
    right_duration = right_end - right_start
    shorter_duration = min(left_duration, right_duration)
    if shorter_duration <= 0:
        return 0.0
    overlap = max(0.0, min(left_end, right_end) - max(left_start, right_start))
    return min(1.0, overlap / shorter_duration)


def clip_duration_ratio(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Return the shorter/longer duration ratio for two valid clip ranges."""
    try:
        durations = (
            float(left["end"]) - float(left["start"]),
            float(right["end"]) - float(right["start"]),
        )
    except (KeyError, TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(value) and value > 0 for value in durations):
        return 0.0
    return min(durations) / max(durations)


def ngram_containment(left: list[str], right: list[str], size: int) -> float:
    """Measure shared n-grams relative to the smaller unique n-gram set."""
    if len(left) < size or len(right) < size:
        return 0.0
    left_ngrams = {
        tuple(left[index : index + size])
        for index in range(len(left) - size + 1)
    }
    right_ngrams = {
        tuple(right[index : index + size])
        for index in range(len(right) - size + 1)
    }
    smaller_count = min(len(left_ngrams), len(right_ngrams))
    if not smaller_count:
        return 0.0
    return len(left_ngrams & right_ngrams) / smaller_count


def duplicate_clip_evidence(
    kept_clip: dict[str, Any], candidate_clip: dict[str, Any]
) -> tuple[str, dict[str, float]] | None:
    """Return evidence when two planned clips cover the same source passage."""
    time_containment = clip_time_containment(kept_clip, candidate_clip)
    duration_ratio = clip_duration_ratio(kept_clip, candidate_clip)
    kept_words = clip_english_words(kept_clip)
    candidate_words = clip_english_words(candidate_clip)
    four_gram_containment = ngram_containment(kept_words, candidate_words, 4)
    five_gram_containment = ngram_containment(kept_words, candidate_words, 5)
    evidence = {
        "time_containment": round(time_containment, 4),
        "duration_ratio": round(duration_ratio, 4),
        "four_gram_containment": round(four_gram_containment, 4),
        "five_gram_containment": round(five_gram_containment, 4),
        "shorter_english_word_count": float(min(len(kept_words), len(candidate_words))),
    }
    if (
        time_containment >= TIME_CONTAINMENT_THRESHOLD
        and duration_ratio >= MIN_TIME_DUPLICATE_DURATION_RATIO
    ):
        return "time_containment", evidence
    if (
        min(len(kept_words), len(candidate_words)) >= MIN_TRANSCRIPT_DUPLICATE_WORDS
        and five_gram_containment >= FIVE_GRAM_CONTAINMENT_THRESHOLD
    ):
        return "english_subtitle_5gram_containment", evidence
    return None


def clip_audit_summary(clip: dict[str, Any]) -> dict[str, Any]:
    return {
        "speaker": str(clip.get("speaker") or ""),
        "title": str(clip.get("title") or ""),
        "start": clip.get("start"),
        "end": clip.get("end"),
        "daily_speaker_index": clip.get("daily_speaker_index"),
    }


def deduplicate_plan_clips(plan: dict[str, Any], *, stage: str) -> list[dict[str, Any]]:
    """Stably remove cross-speaker plans that describe the same source passage."""
    clips = plan.get("clips")
    if not isinstance(clips, list):
        return []

    retained: list[tuple[int, Any]] = []
    removed: list[dict[str, Any]] = []
    for candidate_position, candidate in enumerate(clips, start=1):
        if not isinstance(candidate, dict):
            retained.append((candidate_position, candidate))
            continue
        for kept_position, kept_clip in retained:
            if not isinstance(kept_clip, dict):
                continue
            match = duplicate_clip_evidence(kept_clip, candidate)
            if match is None:
                continue
            reason, evidence = match
            removed.append(
                {
                    "stage": stage,
                    "reason": reason,
                    "kept_position": kept_position,
                    "removed_position": candidate_position,
                    "kept_clip": clip_audit_summary(kept_clip),
                    "removed_clip": clip_audit_summary(candidate),
                    "evidence": evidence,
                }
            )
            break
        else:
            retained.append((candidate_position, candidate))

    plan["clips"] = [clip for _, clip in retained]
    audit = plan.setdefault("deduplicated_clips", [])
    if not isinstance(audit, list):
        audit = []
        plan["deduplicated_clips"] = audit
    audit.extend(removed)
    if removed:
        print(
            f"Removed {len(removed)} duplicate clip(s) during {stage}: "
            + ", ".join(
                f"{item['removed_position']} duplicates {item['kept_position']}"
                for item in removed
            ),
            flush=True,
        )
    return removed


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
        "deduplicated_clips": [],
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
        try:
            return read_title_refinement_status(plan_path)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            plan_path.write_text(original_plan, encoding="utf-8")
            print(
                f"::warning::Title refiner returned no valid status ({exc}); "
                "using planner-generated titles.",
                flush=True,
            )
            return "planner_fallback"

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
    deduplicate_plan_clips(combined, stage="pre_title_refine")
    args.combined_plan.write_text(
        json.dumps(combined, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
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
    deduplicate_plan_clips(
        final_combined,
        stage="post_title_refine" if not args.skip_title_refine else "final_validation",
    )
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

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import plan_daily_highlights  # noqa: E402
import plan_speaker_highlights  # noqa: E402


class PlanDailyHighlightsTests(unittest.TestCase):
    def make_inputs(self, root: Path) -> tuple[Path, Path, Path, Path]:
        transcript = root / "transcript.json"
        transcript.write_text('{"segments": []}\n', encoding="utf-8")
        speakers = root / "speakers.json"
        speakers.write_text(
            json.dumps(
                {
                    "show_date": "2026-07-12",
                    "speakers": [
                        {
                            "speaker": "Guest One",
                            "speaker_context": "Example role",
                            "segment_start": 10,
                            "segment_end": 180,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return transcript, speakers, root / "plans", root / "combined.json"

    def run_main(self, root: Path, *, allow_empty: bool) -> Path:
        transcript, speakers, out_dir, combined = self.make_inputs(root)
        argv = [
            "plan_daily_highlights.py",
            "--transcript",
            str(transcript),
            "--speakers",
            str(speakers),
            "--out-dir",
            str(out_dir),
            "--combined-plan",
            str(combined),
        ]
        if allow_empty:
            argv.append("--allow-empty")
        with mock.patch.object(sys, "argv", argv):
            plan_daily_highlights.main()
        return combined

    def test_sensitive_filter_messages_are_no_eligible_outcomes(self) -> None:
        self.assertTrue(
            plan_daily_highlights.is_no_eligible_clip_output(
                "No non-sensitive-topic clips remained after title refinement"
            )
        )
        self.assertFalse(
            plan_daily_highlights.is_no_eligible_clip_output("DeepSeek API failed: timeout")
        )

    def test_allow_empty_writes_successful_empty_plan(self) -> None:
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=plan_speaker_highlights.NO_ELIGIBLE_CLIPS_EXIT_CODE,
            stdout="All generated clips were sensitive-topic related\n",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            plan_daily_highlights,
            "run_and_stream",
            return_value=proc,
        ):
            combined = self.run_main(Path(tmp), allow_empty=True)
            payload = json.loads(combined.read_text(encoding="utf-8"))

        self.assertEqual(payload["planning_status"], "no_eligible_clips")
        self.assertEqual(payload["clips"], [])

    def test_default_mode_keeps_empty_plan_strict(self) -> None:
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=plan_speaker_highlights.NO_ELIGIBLE_CLIPS_EXIT_CODE,
            stdout="All generated clips failed the speaker-content quality gate\n",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            plan_daily_highlights,
            "run_and_stream",
            return_value=proc,
        ):
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "No clips planned"):
                self.run_main(root, allow_empty=False)
            payload = json.loads((root / "combined.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["planning_status"], "no_eligible_clips")

    def test_allow_empty_does_not_hide_planner_failures(self) -> None:
        proc = subprocess.CompletedProcess(
            args=[],
            returncode=1,
            stdout="DeepSeek API failed: timeout\n",
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            plan_daily_highlights,
            "run_and_stream",
            return_value=proc,
        ):
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "Speaker planner failed"):
                self.run_main(root, allow_empty=True)
            payload = json.loads((root / "combined.json").read_text(encoding="utf-8"))

        self.assertEqual(payload["planning_status"], "planner_failed")

    def test_no_eligible_exit_code_is_structured(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            plan_speaker_highlights.exit_no_eligible_clips("not suitable")
        self.assertEqual(raised.exception.code, plan_speaker_highlights.NO_ELIGIBLE_CLIPS_EXIT_CODE)

    def test_title_refinement_failure_restores_planner_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "combined.json"
            original = {"planning_status": "planned", "clips": [{"title": "Planner title"}]}
            plan.write_text(json.dumps(original), encoding="utf-8")

            def failed_refiner(_command: list[str]) -> subprocess.CompletedProcess[str]:
                plan.write_text('{"clips": [{"title": "Rejected refinement"}]}', encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="Title emotion-polarity quality gate failed\n",
                )

            with mock.patch.object(
                plan_daily_highlights,
                "run_and_stream",
                side_effect=failed_refiner,
            ):
                status = plan_daily_highlights.refine_titles_or_restore_planner_plan(
                    plan,
                    root / "refine_clip_titles.py",
                )

            restored = json.loads(plan.read_text(encoding="utf-8"))

        self.assertEqual(status, "planner_fallback")
        self.assertEqual(restored, original)

    def test_sensitive_title_refinement_outcome_is_not_restored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = root / "combined.json"
            plan.write_text('{"clips": [{"title": "Planner title"}]}', encoding="utf-8")

            def sensitive_refiner(_command: list[str]) -> subprocess.CompletedProcess[str]:
                plan.write_text('{"clips": []}', encoding="utf-8")
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=1,
                    stdout="No non-sensitive-topic clips remained after title refinement\n",
                )

            with mock.patch.object(
                plan_daily_highlights,
                "run_and_stream",
                side_effect=sensitive_refiner,
            ):
                status = plan_daily_highlights.refine_titles_or_restore_planner_plan(
                    plan,
                    root / "refine_clip_titles.py",
                )

            remaining = json.loads(plan.read_text(encoding="utf-8"))

        self.assertEqual(status, "no_eligible_clips")
        self.assertEqual(remaining["clips"], [])

    def test_main_keeps_planned_clips_when_title_quality_gate_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def planner_then_failed_refiner(
                command: list[str],
            ) -> subprocess.CompletedProcess[str]:
                if "plan_speaker_highlights.py" in command[1]:
                    plan_path = Path(command[command.index("--out") + 1])
                    plan_path.write_text(
                        json.dumps(
                            {
                                "clips": [
                                    {
                                        "index": 1,
                                        "start": 20,
                                        "end": 80,
                                        "title": "Planner title",
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="")
                return subprocess.CompletedProcess(
                    args=command,
                    returncode=1,
                    stdout="Title emotion-polarity quality gate failed\n",
                )

            with (
                mock.patch.object(
                    plan_daily_highlights,
                    "run_and_stream",
                    side_effect=planner_then_failed_refiner,
                ),
                mock.patch.object(
                    plan_daily_highlights,
                    "remove_trump_clips_from_plan",
                    return_value=[],
                ),
            ):
                combined = self.run_main(root, allow_empty=True)
                payload = json.loads(combined.read_text(encoding="utf-8"))

        self.assertEqual(payload["planning_status"], "planned")
        self.assertEqual(payload["title_refinement_status"], "planner_fallback")
        self.assertEqual([clip["title"] for clip in payload["clips"]], ["Planner title"])


if __name__ == "__main__":
    unittest.main()

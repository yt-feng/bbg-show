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
    @staticmethod
    def subtitle_clip(
        title: str,
        start: float,
        end: float,
        english: str,
        *,
        speaker_index: int,
    ) -> dict[str, object]:
        return {
            "title": title,
            "speaker": f"Speaker {speaker_index}",
            "daily_speaker_index": speaker_index,
            "start": start,
            "end": end,
            "subtitles": [{"en": english}],
        }

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

    def test_deduplication_reproduces_2026_07_13_six_to_four_result(self) -> None:
        earnings = (
            "The expectations are twenty something percent historically the S and P five hundred "
            "beats those by about four five six percent depending on the quarter which is a punchy "
            "goal let us call it twenty five percent what we largely expect from the market we think "
            "we will hit that which probably means good things for markets for the next couple weeks "
            "given how much money is being spent in artificial intelligence and consumer resilience"
        )
        market_leadership = (
            "The broader market has begun to participate while the technology leaders remain strong "
            "for the near term investors still see earnings support and healthy balance sheets across "
            "the largest companies but changes in employment demand attention before declaring a new "
            "cycle the leadership structure can persist even while other sectors gradually catch up"
        )
        employment = (
            "The latest employment report was softer than expected and revisions changed the picture "
            "but the economy still has time to extend the expansion consumer spending and company "
            "investment remain sufficient to avoid an immediate downturn while policy makers watch "
            "wages participation and hiring intentions over the coming quarters"
        )
        rates = (
            "One additional increase would not break the economy but the central question is why policy "
            "makers should tighten when wage pressure remains limited inflation expectations are stable "
            "and businesses are already responding to higher financing costs across investment decisions"
        )
        plan = {
            "clips": [
                self.subtitle_clip("01", 630.0, 666.3, earnings, speaker_index=1),
                self.subtitle_clip("02", 674.3, 709.3, market_leadership, speaker_index=1),
                self.subtitle_clip("03", 709.3, 744.3, employment, speaker_index=1),
                self.subtitle_clip("04", 768.3, 801.3, rates, speaker_index=1),
                self.subtitle_clip(
                    "05 renamed duplicate",
                    628.3,
                    670.3,
                    earnings + " so consumers have no reason to throttle their spending",
                    speaker_index=3,
                ),
                self.subtitle_clip(
                    "06 renamed duplicate",
                    674.3,
                    720.0,
                    market_leadership
                    + " employment data is an important warning but does not change the short term view",
                    speaker_index=3,
                ),
            ]
        }

        removed = plan_daily_highlights.deduplicate_plan_clips(
            plan, stage="pre_title_refine"
        )

        self.assertEqual([clip["title"] for clip in plan["clips"]], ["01", "02", "03", "04"])
        self.assertEqual(
            [(item["kept_position"], item["removed_position"]) for item in removed],
            [(1, 5), (2, 6)],
        )
        self.assertEqual(plan["deduplicated_clips"], removed)

    def test_deduplication_keeps_adjacent_clips_and_short_common_intro(self) -> None:
        adjacent_left_text = " ".join(f"leftword{index}" for index in range(45))
        adjacent_right_text = " ".join(f"rightword{index}" for index in range(45))
        common_intro = "Welcome back to Bloomberg television here is what matters for markets today"
        plan = {
            "clips": [
                self.subtitle_clip("adjacent left", 100, 135, adjacent_left_text, speaker_index=1),
                self.subtitle_clip("adjacent right", 135, 170, adjacent_right_text, speaker_index=2),
                self.subtitle_clip("short intro one", 200, 235, common_intro, speaker_index=3),
                self.subtitle_clip("short intro two", 240, 275, common_intro, speaker_index=4),
            ]
        }

        removed = plan_daily_highlights.deduplicate_plan_clips(plan, stage="test")

        self.assertEqual(removed, [])
        self.assertEqual(len(plan["clips"]), 4)
        self.assertEqual(plan["deduplicated_clips"], [])

    def test_deduplication_removes_same_time_clip_without_subtitles(self) -> None:
        plan = {
            "clips": [
                {"title": "first", "start": 100, "end": 140},
                {"title": "renamed duplicate", "start": 98, "end": 142},
            ]
        }

        removed = plan_daily_highlights.deduplicate_plan_clips(plan, stage="test")

        self.assertEqual([clip["title"] for clip in plan["clips"]], ["first"])
        self.assertEqual(removed[0]["reason"], "time_containment")

    def test_deduplication_keeps_a_much_longer_clip_that_contains_new_content(self) -> None:
        plan = {
            "clips": [
                {"title": "short excerpt", "start": 100, "end": 130},
                {"title": "long discussion", "start": 90, "end": 240},
            ]
        }

        removed = plan_daily_highlights.deduplicate_plan_clips(plan, stage="test")

        self.assertEqual(removed, [])
        self.assertEqual(len(plan["clips"]), 2)

    def test_deduplication_removes_same_long_subtitles_at_different_times(self) -> None:
        english = " ".join(f"distinctword{index}" for index in range(55))
        plan = {
            "clips": [
                self.subtitle_clip("first", 100, 140, english, speaker_index=1),
                self.subtitle_clip("renamed duplicate", 500, 540, english, speaker_index=2),
            ]
        }

        removed = plan_daily_highlights.deduplicate_plan_clips(plan, stage="test")

        self.assertEqual([clip["title"] for clip in plan["clips"]], ["first"])
        self.assertEqual(removed[0]["reason"], "english_subtitle_5gram_containment")

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

    def test_main_rechecks_duplicates_added_by_title_refinement(self) -> None:
        english = " ".join(
            f"marketword{index}" for index in range(45)
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def planner_then_duplicate_refiner(
                command: list[str],
            ) -> subprocess.CompletedProcess[str]:
                if "plan_speaker_highlights.py" in command[1]:
                    plan_path = Path(command[command.index("--out") + 1])
                    plan_path.write_text(
                        json.dumps(
                            {
                                "clips": [
                                    {
                                        "start": 20,
                                        "end": 80,
                                        "title": "Planner title",
                                        "subtitles": [{"en": english}],
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

                combined_path = Path(command[command.index("--plan") + 1])
                combined = json.loads(combined_path.read_text(encoding="utf-8"))
                duplicate = dict(combined["clips"][0])
                duplicate["title"] = "Different refined title"
                combined["clips"].append(duplicate)
                combined_path.write_text(json.dumps(combined), encoding="utf-8")
                return subprocess.CompletedProcess(args=command, returncode=0, stdout="")

            with (
                mock.patch.object(
                    plan_daily_highlights,
                    "run_and_stream",
                    side_effect=planner_then_duplicate_refiner,
                ),
                mock.patch.object(
                    plan_daily_highlights,
                    "remove_trump_clips_from_plan",
                    return_value=[],
                ),
            ):
                combined = self.run_main(root, allow_empty=True)
                payload = json.loads(combined.read_text(encoding="utf-8"))

        self.assertEqual([clip["title"] for clip in payload["clips"]], ["Planner title"])
        self.assertEqual(len(payload["deduplicated_clips"]), 1)
        self.assertEqual(payload["deduplicated_clips"][0]["stage"], "post_title_refine")


if __name__ == "__main__":
    unittest.main()

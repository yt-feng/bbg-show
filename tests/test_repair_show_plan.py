from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import repair_show_plan as repair
import render_clips_linux as renderer


def clip(speaker: str, start: float, title: str) -> dict:
    return {"speaker": speaker, "start": start, "end": start + 60, "title": title,
            "daily_speaker_index": 1, "subtitles": []}


class ShowRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "rendered-clips" / "2026-10-25"
        self.work = self.root / "work"
        self.old_clip = clip("Dan", 100, "Published title")
        self.speakers = {"selection_status": "selected", "speakers": [
            {"speaker": "Dan", "segment_start": 100, "segment_end": 200},
            {"speaker": "Drew", "segment_start": 300, "segment_end": 400},
            {"speaker": "Rana", "segment_start": 500, "segment_end": 600},
        ]}
        self.old_plan = {"clips": [self.old_clip], "planning_status": "planned", "skipped_speakers": [
            {"speaker": "Drew", "reason": "planner failed with exit code 1"},
            {"speaker": "Rana", "reason": "no eligible clips after content quality filters"},
        ]}
        repair.write_json(self.output / "highlight_plan.json", self.old_plan)
        repair.write_json(self.output / "speakers.json", self.speakers)
        self.identity = {"show_date": "2026-10-25", "asset_id": "old-asset"}
        repair.write_json(self.output / "source_identity.json", self.identity)
        self.old_video = self.output / renderer.output_name(1, self.old_clip)
        self.old_video.write_bytes(b"published-video-bytes")

    def prepare(self) -> None:
        repair.prepare(self.output, self.work, "2026-10-25")

    def write_repaired(self, clips: list[dict], **extra) -> None:
        repair.write_json(self.work / "highlight_plan.json", {
            "clips": clips,
            "planning_status": "planned" if clips else "no_eligible_clips",
            "skipped_speakers": [],
            **extra,
        })

    def test_only_failed_speaker_is_retried_and_original_metadata_is_saved(self) -> None:
        self.prepare()
        selected = repair.read_json(self.work / "repair_speakers.json")
        self.assertEqual([item["speaker"] for item in selected["speakers"]], ["Drew"])
        self.assertEqual(selected["speakers"][0]["segment_start"], 300)
        self.assertEqual(repair.read_json(self.work / "previous_speakers.json"), self.speakers)
        self.assertEqual(repair.read_json(self.output / "highlight_plan.json"), self.old_plan)

    def test_repair_requires_explicit_matching_date_and_existing_published_files(self) -> None:
        for show_date in ("", "20261025", "2026-10-26"):
            with self.subTest(show_date=show_date), self.assertRaises(SystemExit):
                repair.prepare(self.output, self.work, show_date)
        self.old_video.unlink()
        with self.assertRaisesRegex(SystemExit, "Published clip is missing"):
            self.prepare()

    def test_missing_metadata_fails_before_writes(self) -> None:
        (self.output / "speakers.json").unlink()
        with self.assertRaisesRegex(SystemExit, "Cannot read show repair metadata"):
            self.prepare()
        self.assertFalse(self.work.exists())

    def test_downloaded_source_must_match_published_asset(self) -> None:
        self.prepare()
        repair.write_json(self.work / "source_identity.json", self.identity)
        repair.verify_source(self.work)
        repair.write_json(self.work / "source_identity.json", {**self.identity, "asset_id": "different"})
        with self.assertRaisesRegex(SystemExit, "different source"):
            repair.verify_source(self.work)

    def test_merge_appends_new_clip_and_renderer_skips_existing_bytes(self) -> None:
        self.prepare()
        self.write_repaired([clip("德鲁·马图斯", 300, "New title")])
        repair.merge(self.work)
        merged = repair.read_json(self.work / "highlight_plan.json")
        self.assertEqual(merged["clips"][0], self.old_clip)
        self.assertEqual(merged["clips"][1]["daily_speaker_index"], 2)
        self.assertEqual(merged["skipped_speakers"], [self.old_plan["skipped_speakers"][1]])
        self.assertEqual(repair.read_json(self.work / "speakers.json"), self.speakers)
        source = self.root / "source.mp4"
        source.write_bytes(b"source-fixture")

        def fake_render(**kwargs) -> None:
            kwargs["output"].write_bytes(b"new-video-bytes")

        argv = ["render_clips_linux.py", "--source", str(source), "--plan", str(self.work / "highlight_plan.json"),
                "--out-dir", str(self.output), "--work-dir", str(self.work / "render")]
        with patch.object(sys, "argv", argv), patch.object(renderer, "render_overlay_images", return_value=(Path("static.png"), [])), patch.object(renderer, "render_clip", side_effect=fake_render) as render:
            renderer.main()
        render.assert_called_once()
        self.assertEqual(self.old_video.read_bytes(), b"published-video-bytes")
        self.assertEqual((self.output / renderer.output_name(2, merged["clips"][1])).read_bytes(), b"new-video-bytes")

    def test_empty_eligible_repair_removes_failed_marker_and_preserves_old_clip(self) -> None:
        self.prepare()
        self.write_repaired([], skipped_speakers=[{"speaker": "Drew", "reason": "no eligible clips after content quality filters"}])
        repair.merge(self.work)
        merged = repair.read_json(self.work / "highlight_plan.json")
        self.assertEqual(merged["clips"], [self.old_clip])
        self.assertFalse(repair.failed_names(merged))
        self.assertEqual(merged["planning_status"], "planned")

    def test_repeated_passage_is_not_published_twice(self) -> None:
        self.prepare()
        self.write_repaired([clip("Drew", 100, "Same passage, other title"), clip("Drew", 300, "New passage")])
        repair.merge(self.work)
        merged = repair.read_json(self.work / "highlight_plan.json")
        self.assertEqual(len(merged["clips"]), 2)
        self.assertEqual(merged["clips"][0], self.old_clip)
        self.assertEqual(merged["clips"][1]["start"], 300)

    def test_partial_failure_cannot_replace_previous_plan(self) -> None:
        self.prepare()
        self.write_repaired([clip("Drew", 300, "Draft")], planning_status="planner_failed")
        with self.assertRaisesRegex(SystemExit, "failed or incomplete"):
            repair.merge(self.work)
        self.assertEqual(repair.read_json(self.output / "highlight_plan.json"), self.old_plan)


if __name__ == "__main__":
    unittest.main()

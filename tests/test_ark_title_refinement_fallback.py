from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import process_ark_videos  # noqa: E402


class ArkTitleRefinementFallbackTests(unittest.TestCase):
    def test_sensitive_skip_detection_requires_a_terminal_marker(self) -> None:
        mixed_technical_failure = (
            "Removed 1 sensitive-topic clip(s) before title refinement\n"
            "DeepSeek request timed out"
        )

        self.assertFalse(process_ark_videos.is_sensitive_skip_output(mixed_technical_failure))
        self.assertTrue(
            process_ark_videos.is_sensitive_skip_output(
                "No non-sensitive-topic clips remained after title refinement"
            )
        )

    def test_technical_failure_restores_planner_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            original = '{"clips": [{"title": "planner title"}]}\n'
            plan_path.write_text(original, encoding="utf-8")
            error = subprocess.CalledProcessError(1, ["refiner"])

            with mock.patch.object(process_ark_videos, "run", side_effect=error):
                status = process_ark_videos.refine_title_or_keep_planner_title(plan_path)

            self.assertEqual(status, "planner_fallback")
            self.assertEqual(plan_path.read_text(encoding="utf-8"), original)

    def test_sensitive_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text('{"clips": []}', encoding="utf-8")
            with (
                mock.patch.object(
                    process_ark_videos,
                    "run",
                    side_effect=RuntimeError("ARK video skipped by sensitive topic filter"),
                ),
                self.assertRaisesRegex(RuntimeError, "sensitive topic filter"),
            ):
                process_ark_videos.refine_title_or_keep_planner_title(plan_path)

    def test_process_metadata_preserves_partial_refinement_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(
                work_root=root / "work",
                search_results=5,
                min_video_seconds=30.0,
                max_clip_seconds=110.0,
                threads=1,
                whisper_model="base",
            )

            def fake_run(command: list[str], *_args: object, **_kwargs: object) -> None:
                if "transcribe_video.py" in command[1]:
                    Path(command[command.index("--out") + 1]).write_text(
                        '{"segments": []}',
                        encoding="utf-8",
                    )
                elif "plan_top_video_full.py" in command[1]:
                    Path(command[command.index("--out") + 1]).write_text(
                        json.dumps({"clips": [{"title": "Planner title"}]}),
                        encoding="utf-8",
                    )
                elif "render_clips_linux.py" in command[1]:
                    render_dir = Path(command[command.index("--out-dir") + 1])
                    (render_dir / "clip.mp4").write_bytes(b"video")

            with (
                mock.patch.object(
                    process_ark_videos,
                    "resolve_youtube_url",
                    return_value={
                        "url": "https://youtube.com/watch?v=test",
                        "title": "YouTube title",
                        "channel": "ARK Invest",
                    },
                ),
                mock.patch.object(process_ark_videos, "download_video"),
                mock.patch.object(process_ark_videos, "ffprobe_duration", return_value=600.0),
                mock.patch.object(process_ark_videos, "run", side_effect=fake_run),
                mock.patch.object(
                    process_ark_videos,
                    "refine_title_or_keep_planner_title",
                    return_value="partial_refined",
                ),
                mock.patch.object(process_ark_videos, "remove_trump_clips_from_plan", return_value=[]),
            ):
                result = process_ark_videos.process_one(
                    {
                        "url": "https://ark-invest.com/videos/test",
                        "title": "Source title",
                        "slug": "source-title",
                        "speaker": "Cathie Wood",
                    },
                    1,
                    args,
                    root / "rendered" / "2026-07-19",
                )

            video_json = json.loads(
                (root / "rendered" / "2026-07-19" / "ark-invest" / "01_source-title" / "video.json")
                .read_text(encoding="utf-8")
            )

        self.assertEqual(result["title_refinement_status"], "partial_refined")
        self.assertEqual(video_json["title_refinement_status"], "partial_refined")


if __name__ == "__main__":
    unittest.main()

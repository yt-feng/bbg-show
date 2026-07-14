from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import collect_top_video_outputs  # noqa: E402
import process_top_videos  # noqa: E402
from content_fingerprint import fingerprint_text  # noqa: E402


def substantial_text(prefix: str, count: int = 320) -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


def clip_record(text: str, *, title: str = "Clip") -> dict[str, object]:
    return {
        "clip_index": 1,
        "title": title,
        "start": 0.0,
        "end": 60.0,
        "duration": 60.0,
        "clip_fingerprint": fingerprint_text(text),
    }


class TopContentDedupPipelineTests(unittest.TestCase):
    def write_success(
        self,
        output_dir: Path,
        *,
        index: int,
        slug: str,
        source_text: str,
        clip_text: str,
        url: str,
    ) -> Path:
        render_dir = output_dir / f"{index:02d}_{slug}"
        render_dir.mkdir(parents=True)
        (render_dir / "clip.mp4").write_bytes(b"video")
        item = {
            "status": "success",
            "index": index,
            "url": url,
            "title": f"Title {index}",
            "source_title": f"Source {index}",
            "source": "bloomberg",
            "youtube_id": "",
            "duration": 600.0,
            "source_duration": 600.0,
            "source_fingerprint": fingerprint_text(source_text),
            "clip_fingerprints": [clip_record(clip_text, title=f"Clip {index}")],
            "output_dir": str(render_dir),
            "rendered_files": ["clip.mp4"],
        }
        summary = {
            "run_date": "2026-07-15",
            "video_index": index,
            "total": 1,
            "succeeded": 1,
            "failed": 0,
            "skipped": 0,
            "videos": [item],
        }
        path = output_dir / f"summary_{index:02d}.json"
        path.write_text(json.dumps(summary), encoding="utf-8")
        return path

    def test_source_duplicate_stops_before_planning_and_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "output"
            args = argparse.Namespace(
                work_root=root / "work",
                download_backend="auto",
                workers=2,
                threads=1,
                whisper_model="base",
                min_video_seconds=15.0,
                max_clip_seconds=90.0,
                processed_sources=root / "ledger.json",
            )
            commands: list[list[str]] = []

            def fake_run(command: list[str], *args_: object, **kwargs: object) -> None:
                commands.append(command)
                if "transcribe_video.py" in command[1]:
                    out = Path(command[command.index("--out") + 1])
                    out.write_text(
                        json.dumps({"segments": [{"text": substantial_text("source")}]},),
                        encoding="utf-8",
                    )

            duplicate = {
                "duplicate_kind": "full_source",
                "duplicate_of": "2026-07-14 · old title",
            }
            with (
                mock.patch.object(process_top_videos, "is_trump_related", return_value=False),
                mock.patch.object(process_top_videos, "run", side_effect=fake_run),
                mock.patch.object(process_top_videos, "ffprobe_duration", return_value=600.0),
                mock.patch.object(
                    process_top_videos,
                    "find_duplicate_content",
                    return_value=duplicate,
                ),
            ):
                result = process_top_videos.process_one(
                    {
                        "url": "https://www.bloomberg.com/news/videos/new-name-video",
                        "title": "New title",
                        "slug": "new_name",
                    },
                    1,
                    args,
                    output_dir,
                )

        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["skip_reason"], "duplicate_content")
        self.assertEqual(result["duplicate_stage"], "source_transcript")
        self.assertFalse(any("plan_top_video_full.py" in command[1] for command in commands))
        self.assertFalse(any("render_clips_linux.py" in command[1] for command in commands))

    def test_collector_keeps_first_same_batch_success_and_skips_later_index(self) -> None:
        source = substantial_text("same-source")
        clip = substantial_text("same-clip", 80)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "2026-07-15"
            output_dir.mkdir()
            first = self.write_success(
                output_dir,
                index=1,
                slug="first",
                source_text=source,
                clip_text=clip,
                url="https://www.bloomberg.com/news/videos/first-name-video",
            )
            second = self.write_success(
                output_dir,
                index=2,
                slug="renamed",
                source_text=source,
                clip_text=clip,
                url="https://www.bloomberg.com/news/videos/different-name-video",
            )
            ledger = root / "processed_sources.json"

            collect_top_video_outputs.prune_unsuccessful_outputs(output_dir, expected_total=2)
            result = collect_top_video_outputs.deduplicate_success_outputs(output_dir, ledger)

            first_item = json.loads(first.read_text(encoding="utf-8"))["videos"][0]
            second_item = json.loads(second.read_text(encoding="utf-8"))["videos"][0]
            self.assertEqual(result, {"accepted": 1, "duplicates": 1, "invalid_fingerprints": 0})
            self.assertEqual(first_item["status"], "success")
            self.assertEqual(second_item["status"], "skipped")
            self.assertEqual(second_item["skip_reason"], "duplicate_content")
            self.assertIn("Title 1", second_item["duplicate_of"])
            self.assertTrue((output_dir / "01_first").is_dir())
            self.assertFalse((output_dir / "02_renamed").exists())

    def test_collector_defensively_skips_clip_found_in_history(self) -> None:
        historical_clip = substantial_text("published-clip", 80)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "2026-07-15"
            output_dir.mkdir()
            summary_path = self.write_success(
                output_dir,
                index=1,
                slug="renamed",
                source_text=substantial_text("new-source"),
                clip_text=historical_clip,
                url="https://www.bloomberg.com/news/videos/renamed-video",
            )
            ledger = root / "processed_sources.json"
            ledger.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "sources": [],
                        "clips": [
                            {
                                "source_key": "url:https://www.bloomberg.com/news/videos/old-video",
                                "url": "https://www.bloomberg.com/news/videos/old-video",
                                "source_title": "Previously published title",
                                "processed_on": "2026-06-25",
                                "clip_index": 1,
                                "clip_fingerprint": fingerprint_text(historical_clip),
                            }
                        ],
                        "history_backfilled": True,
                    }
                ),
                encoding="utf-8",
            )

            collect_top_video_outputs.prune_unsuccessful_outputs(output_dir, expected_total=1)
            result = collect_top_video_outputs.deduplicate_success_outputs(output_dir, ledger)
            item = json.loads(summary_path.read_text(encoding="utf-8"))["videos"][0]

        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(item["status"], "skipped")
        self.assertEqual(item["duplicate_kind"], "clip")
        self.assertIn("Previously published title", item["duplicate_of"])

    def test_workflow_passes_ledger_and_backfills_complete_git_history(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "daily-top-videos.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--backfill-git-history", workflow)
        self.assertIn("--deduplicate-content", workflow)
        self.assertGreaterEqual(
            workflow.count("--processed-sources rendered-clips/top-videos/processed_sources.json"),
            4,
        )


if __name__ == "__main__":
    unittest.main()

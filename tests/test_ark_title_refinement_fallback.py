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
    def test_ytdlp_search_enables_node_runtime_and_pot_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            candidate = {
                "id": "COmxq7bh-fM",
                "url": "COmxq7bh-fM",
                "webpage_url": "https://www.youtube.com/watch?v=COmxq7bh-fM",
                "ie_key": "Youtube",
                "title": "Why This Jobs Report Might Be Good News",
                "channel": "ARK Invest",
            }
            completed = subprocess.CompletedProcess(
                ["yt-dlp"],
                0,
                stdout=json.dumps(candidate) + "\n",
                stderr="",
            )
            with (
                mock.patch.object(process_ark_videos, "ytdlp_command", return_value=["yt-dlp"]),
                mock.patch.object(process_ark_videos.subprocess, "run", return_value=completed) as run,
                mock.patch.object(process_ark_videos, "is_trump_related", return_value=False),
            ):
                selected = process_ark_videos.resolve_youtube_url(
                    {
                        "download_query": "ARK Invest Cathie Wood jobs report",
                        "title": "Why This Jobs Report Might Be Good News",
                    },
                    work_dir,
                    5,
                    "node",
                    "http://127.0.0.1:4416/",
                )

        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["--js-runtimes", "node"])
        self.assertIn("youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416", command)
        self.assertEqual(selected["url"], "https://www.youtube.com/watch?v=COmxq7bh-fM")

    def test_ytdlp_download_tries_mweb_with_node_runtime_and_pot_provider(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "work"
            work_dir.mkdir()
            output = Path(tmp) / "video.mp4"
            args = argparse.Namespace(
                yt_dlp_proxy_mode="never",
                yt_dlp_js_runtime="node",
                yt_dlp_pot_provider_url="http://127.0.0.1:4416",
            )
            with (
                mock.patch.object(process_ark_videos, "ytdlp_command", return_value=["yt-dlp"]),
                mock.patch.object(process_ark_videos, "run") as run,
            ):
                process_ark_videos.download_video(
                    "https://www.youtube.com/watch?v=COmxq7bh-fM",
                    output,
                    work_dir,
                    args,
                )

        command = run.call_args.args[0]
        self.assertEqual(command[1:3], ["--js-runtimes", "node"])
        self.assertIn("youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416", command)
        self.assertIn("youtube:player_client=mweb", command)

    def test_ytdlp_download_falls_back_after_mweb_without_empty_provider_arg(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp) / "work"
            work_dir.mkdir()
            output = Path(tmp) / "video.mp4"
            args = argparse.Namespace(
                yt_dlp_proxy_mode="never",
                yt_dlp_js_runtime="node",
                yt_dlp_pot_provider_url="",
            )
            mweb_failure = subprocess.CalledProcessError(1, ["yt-dlp"])
            with (
                mock.patch.object(process_ark_videos, "ytdlp_command", return_value=["yt-dlp"]),
                mock.patch.object(
                    process_ark_videos,
                    "run",
                    side_effect=[mweb_failure, None],
                ) as run,
            ):
                process_ark_videos.download_video(
                    "https://www.youtube.com/watch?v=COmxq7bh-fM",
                    output,
                    work_dir,
                    args,
                )

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertIn("youtube:player_client=mweb", commands[0])
        self.assertNotIn("youtube:player_client=mweb", commands[1])
        self.assertFalse(
            any("youtubepot-bgutilhttp" in argument for command in commands for argument in command)
        )

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
                ark_chrome_bin="",
                wistia_max_height=720,
                yt_dlp_js_runtime="node",
                yt_dlp_pot_provider_url="http://127.0.0.1:4416",
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
                    "resolve_ark_source",
                    return_value={
                        "provider": "youtube",
                        "url": "https://youtube.com/watch?v=test",
                        "title": "YouTube title",
                        "channel": "ARK Invest",
                    },
                ),
                mock.patch.object(process_ark_videos, "download_selected_source"),
                mock.patch.object(process_ark_videos, "validate_downloaded_source", return_value=600.0),
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

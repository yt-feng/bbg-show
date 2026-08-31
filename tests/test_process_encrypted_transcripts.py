from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import process_ark_videos  # noqa: E402
import process_top_videos  # noqa: E402


class ArkEncryptedTranscriptTests(unittest.TestCase):
    def test_archives_arbitrary_success_transcript_paths_with_stable_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work_root = root / "work"
            archive_root = root / "transcripts"
            recipient_cert = root / "recipient.pem"
            wistia_transcript = root / "captions" / "official-normalized.json"
            whisper_transcript = root / "asr" / "nested" / "whisper-result.json"
            successes = [
                {
                    "status": "success",
                    "index": 1,
                    "transcript": str(wistia_transcript),
                    "guid": "https://ark.example/guid/wistia",
                    "media_provider": "wistia",
                    "pub_date": "2026-08-30T12:00:00Z",
                    "source_media_url": "https://fast.wistia.example/source.mp4",
                    "source_title": "Official caption source",
                    "url": "https://ark.example/videos/wistia",
                    "wistia_id": "abc123def4",
                    "youtube_url": "",
                    "title": "AI-refined title must not enter source identity",
                    "video_file": "/runner/private/source.mp4",
                    "output_dir": "/runner/rendered/output",
                    "rendered_files": ["clip.mp4"],
                },
                {
                    "status": "success",
                    "index": 2,
                    "transcript": str(whisper_transcript),
                    "guid": "https://ark.example/guid/youtube",
                    "media_provider": "youtube",
                    "pub_date": "2026-08-29T12:00:00Z",
                    "source_media_url": "https://youtube.example/watch?v=source",
                    "source_title": "Whisper source",
                    "url": "https://ark.example/videos/youtube",
                    "wistia_id": "",
                    "youtube_url": "https://youtube.example/watch?v=source",
                    "description": "Mutable feed description",
                    "transcript_model": "base",
                },
            ]
            calls: list[dict[str, object]] = []

            def fake_archive(
                transcript_path: Path,
                source_path: Path,
                cert_path: Path,
                output_dir: Path,
            ) -> SimpleNamespace:
                number = len(calls) + 1
                output_dir.mkdir(parents=True, exist_ok=True)
                json_cms = output_dir / f"archive-{number}.json.cms"
                markdown_cms = output_dir / f"archive-{number}.md.cms"
                json_cms.write_bytes(b"json-ciphertext")
                markdown_cms.write_bytes(b"markdown-ciphertext")
                calls.append(
                    {
                        "transcript": Path(transcript_path),
                        "source": json.loads(Path(source_path).read_text(encoding="utf-8")),
                        "source_path": Path(source_path),
                        "recipient_cert": Path(cert_path),
                        "output_dir": Path(output_dir),
                    }
                )
                return SimpleNamespace(json_cms=json_cms, markdown_cms=markdown_cms)

            with mock.patch.object(
                process_ark_videos,
                "archive_transcript",
                side_effect=fake_archive,
            ):
                archived = process_ark_videos.archive_successful_transcripts(
                    successes,
                    run_date="2026-08-31",
                    work_root=work_root,
                    archive_root=archive_root,
                    recipient_cert=recipient_cert,
                )

        self.assertEqual(archived, 2)
        self.assertEqual(
            [call["transcript"] for call in calls],
            [wistia_transcript, whisper_transcript],
        )
        expected_keys = {
            "guid",
            "kind",
            "media_provider",
            "pub_date",
            "source_media_url",
            "source_title",
            "url",
            "wistia_id",
            "youtube_url",
        }
        for call in calls:
            self.assertEqual(set(call["source"]), expected_keys)
            self.assertEqual(call["source"]["kind"], "ark-invest")
            self.assertEqual(call["recipient_cert"], recipient_cert)
            self.assertEqual(
                call["output_dir"],
                archive_root / "ark-invest" / "2026-08-31",
            )
            self.assertEqual(call["source_path"].parent, work_root / "transcript-archive-sources")

        for result in successes:
            names = result["encrypted_transcript_archives"]
            self.assertEqual(len(names), 2)
            self.assertTrue(all(Path(name).name == name for name in names))
            self.assertTrue(all(name.endswith(".cms") for name in names))

    def test_main_passes_only_success_results_to_transcript_archiver(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text("{}\n", encoding="utf-8")
            successful = {
                "status": "success",
                "index": 1,
                "url": "https://ark.example/videos/success",
                "source_title": "Successful source",
                "transcript": str(root / "arbitrary" / "success.json"),
            }
            candidates = [
                {"url": "https://ark.example/videos/success", "title": "Success"},
                {"url": "https://ark.example/videos/failure", "title": "Failure"},
            ]
            argv = [
                "process_ark_videos.py",
                "--manifest",
                str(manifest),
                "--run-date",
                "2026-08-31",
                "--max-videos",
                "2",
                "--out-root",
                str(root / "rendered"),
                "--work-root",
                str(root / "work"),
                "--state",
                str(root / "state.json"),
                "--wistia-source-ledger",
                str(root / "wistia.json"),
                "--transcript-recipient-cert",
                str(root / "recipient.pem"),
                "--transcript-archive-root",
                str(root / "transcripts"),
            ]

            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(process_ark_videos, "load_manifest", return_value=candidates),
                mock.patch.object(
                    process_ark_videos,
                    "process_one",
                    side_effect=[successful, RuntimeError("render failed")],
                ),
                mock.patch.object(
                    process_ark_videos,
                    "archive_successful_transcripts",
                    return_value=1,
                ) as archive_successes,
                mock.patch.object(process_ark_videos, "update_state") as update_state,
                mock.patch.object(process_ark_videos, "update_wistia_source_ledger"),
            ):
                process_ark_videos.main()

        archived_results = archive_successes.call_args.args[0]
        self.assertEqual(archived_results, [successful])
        self.assertTrue(all(item.get("status") == "success" for item in archived_results))
        self.assertEqual(update_state.call_args.args[1], [successful])


class TopEncryptedTranscriptTests(unittest.TestCase):
    def test_success_stages_encrypted_pair_and_records_only_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "rendered" / "2026-08-31"
            recipient_cert = root / "recipient.pem"
            args = argparse.Namespace(
                work_root=root / "work",
                download_backend="auto",
                workers=2,
                threads=1,
                whisper_model="base",
                min_video_seconds=15.0,
                max_clip_seconds=90.0,
                processed_sources=root / "processed_sources.json",
                transcript_recipient_cert=recipient_cert,
            )
            item = {
                "url": "https://www.bloomberg.com/news/videos/test-video",
                "title": "Stable source title",
                "slug": "test-video",
                "source": "bloomberg",
                "youtube_id": "youtube-123",
                "channel_id": "channel-456",
                "published_at": "2026-08-31T00:00:00Z",
                "description": "Mutable description excluded from source identity",
            }
            archive_calls: list[dict[str, object]] = []

            def fake_run(command: list[str], *_args: object, **_kwargs: object) -> None:
                if "transcribe_video.py" in command[1]:
                    transcript_path = Path(command[command.index("--out") + 1])
                    transcript_path.write_text(
                        json.dumps(
                            {
                                "duration": 600.0,
                                "segments": [
                                    {
                                        "start": 0.0,
                                        "end": 60.0,
                                        "text": "Source transcript words for the encrypted archive test.",
                                    }
                                ],
                            }
                        ),
                        encoding="utf-8",
                    )
                elif "plan_top_video_full.py" in command[1]:
                    plan_path = Path(command[command.index("--out") + 1])
                    plan_path.write_text(
                        json.dumps(
                            {
                                "clips": [
                                    {
                                        "start": 0.0,
                                        "end": 60.0,
                                        "title": "Rendered title",
                                        "subtitles": [
                                            {
                                                "en": "Complete source language subtitle for fingerprinting.",
                                                "zh": "完整字幕",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                elif "render_clips_linux.py" in command[1]:
                    render_dir = Path(command[command.index("--out-dir") + 1])
                    (render_dir / "clip.mp4").write_bytes(b"rendered-video")

            def fake_archive(
                transcript_path: Path,
                source_path: Path,
                cert_path: Path,
                archive_dir: Path,
            ) -> SimpleNamespace:
                archive_dir.mkdir(parents=True, exist_ok=True)
                json_cms = archive_dir / "opaque.json.cms"
                markdown_cms = archive_dir / "opaque.md.cms"
                json_cms.write_bytes(b"json-ciphertext")
                markdown_cms.write_bytes(b"markdown-ciphertext")
                archive_calls.append(
                    {
                        "transcript": Path(transcript_path),
                        "source": json.loads(Path(source_path).read_text(encoding="utf-8")),
                        "recipient_cert": Path(cert_path),
                        "archive_dir": Path(archive_dir),
                    }
                )
                return SimpleNamespace(json_cms=json_cms, markdown_cms=markdown_cms)

            with (
                mock.patch.object(process_top_videos, "is_trump_related", return_value=False),
                mock.patch.object(process_top_videos, "run", side_effect=fake_run),
                mock.patch.object(process_top_videos, "ffprobe_duration", return_value=600.0),
                mock.patch.object(process_top_videos, "find_duplicate_content", return_value=None),
                mock.patch.object(
                    process_top_videos,
                    "refine_title_or_keep_planner_title",
                    return_value="refined",
                ),
                mock.patch.object(process_top_videos, "remove_trump_clips_from_plan", return_value=[]),
                mock.patch.object(
                    process_top_videos,
                    "archive_transcript",
                    side_effect=fake_archive,
                ),
            ):
                result = process_top_videos.process_one(item, 1, args, output_dir)

            render_dir = output_dir / "01_test-video"
            video_metadata = json.loads((render_dir / "video.json").read_text(encoding="utf-8"))
            staged_files_exist = [
                (render_dir / "_transcript_archive" / name).is_file()
                for name in ("opaque.json.cms", "opaque.md.cms")
            ]

        self.assertEqual(result["status"], "success")
        self.assertEqual(len(archive_calls), 1)
        archive_call = archive_calls[0]
        self.assertEqual(archive_call["recipient_cert"], recipient_cert)
        self.assertEqual(archive_call["archive_dir"], render_dir / "_transcript_archive")
        self.assertEqual(
            set(archive_call["source"]),
            {
                "channel_id",
                "kind",
                "published_at",
                "source",
                "source_title",
                "url",
                "youtube_id",
            },
        )

        expected_names = ["opaque.json.cms", "opaque.md.cms"]
        self.assertEqual(result["encrypted_transcript_archives"], expected_names)
        self.assertEqual(video_metadata["encrypted_transcript_archives"], expected_names)
        self.assertTrue(all(Path(name).name == name for name in expected_names))
        self.assertEqual(staged_files_exist, [True, True])


if __name__ == "__main__":
    unittest.main()

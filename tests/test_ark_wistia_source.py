from __future__ import annotations

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

import process_ark_videos  # noqa: E402


class ArkWistiaSourceTests(unittest.TestCase):
    def test_extract_wistia_media_id_from_official_embed_variants(self) -> None:
        self.assertEqual(
            process_ark_videos.extract_wistia_media_id(
                '<span class="wistia_embed wistia_async_s9r5knrzoe"></span>'
            ),
            "s9r5knrzoe",
        )
        self.assertEqual(
            process_ark_videos.extract_wistia_media_id(
                "https://fast.wistia.com/embed/medias/s9r5knrzoe.m3u8"
            ),
            "s9r5knrzoe",
        )

    def test_resolve_wistia_source_uses_reader_then_selects_720p(self) -> None:
        page_url = (
            "https://www.ark-invest.com/videos/market-commentary/"
            "august-2026-in-the-know-cathie-wood"
        )
        metadata_url = "https://fast.wistia.com/embed/medias/s9r5knrzoe.json"
        metadata = {
            "media": {
                "hashedId": "s9r5knrzoe",
                "name": "ITK August",
                "duration": 3690.77,
                "protected": False,
                "hls_enabled": True,
                "assets": [
                    {
                        "type": "hd_mp4_video",
                        "height": 1080,
                        "bitrate": 2200,
                        "size": 1000,
                        "public": True,
                        "status": 2,
                        "url": "https://embed-ssl.wistia.com/deliveries/1080.bin",
                    },
                    {
                        "type": "hd_mp4_video",
                        "height": 720,
                        "bitrate": 1400,
                        "size": 700,
                        "public": True,
                        "status": 2,
                        "url": "https://embed-ssl.wistia.com/deliveries/720.bin",
                    },
                    {
                        "type": "md_mp4_video",
                        "height": 540,
                        "bitrate": 900,
                        "size": 500,
                        "public": True,
                        "status": 2,
                        "url": "https://embed-ssl.wistia.com/deliveries/540.bin",
                    },
                ],
            }
        }

        def fake_fetch(url: str, timeout: int = 0) -> str:
            del timeout
            if url == page_url:
                raise process_ark_videos.FetchError("HTTP 403")
            if url == f"https://r.jina.ai/{page_url}":
                return "Video: https://fast.wistia.com/embed/medias/s9r5knrzoe.m3u8"
            if url == metadata_url:
                return json.dumps(metadata)
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            work_dir = Path(tmp)
            with mock.patch.object(process_ark_videos, "fetch_text_direct", side_effect=fake_fetch):
                source = process_ark_videos.resolve_wistia_source(
                    {
                        "url": page_url,
                        "title": "August In The Know With Cathie Wood",
                    },
                    work_dir,
                    max_height=720,
                )

            self.assertTrue((work_dir / "ark_page_reader.txt").exists())
            self.assertTrue((work_dir / "wistia_media.json").exists())

        self.assertEqual(source["provider"], "wistia")
        self.assertEqual(source["media_id"], "s9r5knrzoe")
        self.assertEqual(source["height"], 720)
        self.assertEqual(
            source["url"],
            "https://embed-ssl.wistia.com/deliveries/720.bin",
        )

    def test_multiple_wistia_ids_select_the_item_bound_media(self) -> None:
        page_url = (
            "https://www.ark-invest.com/videos/market-commentary/"
            "august-2026-in-the-know-cathie-wood"
        )

        def metadata(media_id: str, name: str, created_at: int) -> str:
            return json.dumps({
                "media": {
                    "hashedId": media_id,
                    "name": name,
                    "createdAt": created_at,
                    "duration": 3600,
                    "protected": False,
                    "hls_enabled": True,
                    "assets": [{
                        "type": "hd_mp4_video",
                        "height": 720,
                        "bitrate": 1400,
                        "size": 700,
                        "public": True,
                        "status": 2,
                        "url": f"https://embed-ssl.wistia.com/deliveries/{media_id}.bin",
                    }],
                }
            })

        def fake_fetch(url: str, timeout: int = 0) -> str:
            del timeout
            if url == page_url:
                raise process_ark_videos.FetchError("HTTP 403")
            if url == f"https://r.jina.ai/{page_url}":
                return (
                    "https://fast.wistia.com/embed/medias/wrong12345.m3u8\n"
                    "https://fast.wistia.com/embed/medias/s9r5knrzoe.m3u8"
                )
            if url.endswith("/wrong12345.json"):
                return metadata("wrong12345", "Unrelated Robinhood Promo", 1783000000)
            if url.endswith("/s9r5knrzoe.json"):
                return metadata("s9r5knrzoe", "ITK August", 1786152080)
            raise AssertionError(f"unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(process_ark_videos, "fetch_text_direct", side_effect=fake_fetch):
                source = process_ark_videos.resolve_wistia_source(
                    {
                        "url": page_url,
                        "title": 'Why This "Scary" Jobs Report Might Be Good News | ITK With Cathie Wood',
                        "pub_date": "2026-08-08T09:00:00+08:00",
                    },
                    Path(tmp),
                    max_height=720,
                )

        self.assertEqual(source["media_id"], "s9r5knrzoe")
        self.assertGreater(source["identity_score"], 0)

    def test_zero_binding_single_candidate_is_rejected(self) -> None:
        item = {
            "url": "https://www.ark-invest.com/videos/market-commentary/current-item",
            "title": "Current ARK Research Update",
            "pub_date": "2026-08-08T09:00:00+08:00",
        }
        media = {
            "hashedId": "footer1234",
            "name": "Unrelated Footer Promo",
            "createdAt": 1786152080,
            "protected": False,
            "hls_enabled": True,
            "assets": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    process_ark_videos,
                    "discover_ark_wistia_media_ids",
                    return_value=["footer1234"],
                ),
                mock.patch.object(
                    process_ark_videos,
                    "fetch_wistia_media",
                    return_value=(media, json.dumps({"media": media}), "https://metadata"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "no strong title/series binding"):
                    process_ark_videos.resolve_wistia_source(item, Path(tmp))

    def test_near_scoring_multiple_candidates_are_rejected(self) -> None:
        item = {
            "url": "https://www.ark-invest.com/videos/market-commentary/august-itk",
            "title": "August In The Know Jobs Update",
            "pub_date": "2026-08-08T09:00:00+08:00",
        }

        def fake_media(media_id: str, name: str) -> tuple[dict[str, object], str, str]:
            media: dict[str, object] = {
                "hashedId": media_id,
                "name": name,
                "seoDescription": "An In The Know video",
                "createdAt": 1786152080,
                "protected": False,
                "hls_enabled": True,
                "assets": [],
            }
            return media, json.dumps({"media": media}), f"https://metadata/{media_id}"

        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    process_ark_videos,
                    "discover_ark_wistia_media_ids",
                    return_value=["candidate1", "candidate2"],
                ),
                mock.patch.object(
                    process_ark_videos,
                    "fetch_wistia_media",
                    side_effect=[
                        fake_media("candidate1", "August ITK Jobs"),
                        fake_media("candidate2", "August ITK Update"),
                    ],
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "Ambiguous Wistia media identity"):
                    process_ark_videos.resolve_wistia_source(item, Path(tmp))

    def test_download_wistia_video_uses_progressive_asset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "source.mp4"

            def fake_run(command: list[str], *_args: object, **_kwargs: object) -> None:
                destination = Path(command[command.index("--output") + 1])
                destination.write_bytes(b"mp4")

            with (
                mock.patch.object(process_ark_videos, "run", side_effect=fake_run) as run,
                mock.patch.object(process_ark_videos, "ffprobe_duration", return_value=600.0),
            ):
                process_ark_videos.download_wistia_video(
                    {
                        "asset_url": "https://embed-ssl.wistia.com/deliveries/source.bin",
                        "hls_url": "https://fast.wistia.com/embed/medias/test.m3u8",
                    },
                    output,
                    root,
                    720,
                    30.0,
                )
            self.assertEqual(output.read_bytes(), b"mp4")

        command = run.call_args.args[0]
        self.assertEqual(command[0], "curl")

    def test_invalid_progressive_asset_falls_back_to_wistia_hls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "source.mp4"

            def fake_run(command: list[str], *_args: object, **_kwargs: object) -> None:
                if command[0] == "curl":
                    Path(command[command.index("--output") + 1]).write_bytes(b"not-video")
                else:
                    Path(command[command.index("-o") + 1]).write_bytes(b"mp4")

            with (
                mock.patch.object(process_ark_videos, "run", side_effect=fake_run) as run,
                mock.patch.object(
                    process_ark_videos,
                    "ffprobe_duration",
                    side_effect=[RuntimeError("invalid media"), 600.0],
                ),
                mock.patch.object(process_ark_videos, "ytdlp_command", return_value=["yt-dlp"]),
            ):
                process_ark_videos.download_wistia_video(
                    {
                        "asset_url": "https://embed-ssl.wistia.com/deliveries/source.bin",
                        "hls_url": "https://fast.wistia.com/embed/medias/test.m3u8",
                    },
                    output,
                    root,
                    720,
                    30.0,
                )
            self.assertEqual(output.read_bytes(), b"mp4")

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][0], "curl")
        self.assertEqual(commands[1][0], "yt-dlp")

    def test_complete_wistia_failure_falls_back_to_youtube(self) -> None:
        source = {
            "provider": "wistia",
            "media_id": "s9r5knrzoe",
            "url": "https://embed-ssl.wistia.com/deliveries/source.bin",
        }
        youtube = {
            "url": "https://www.youtube.com/watch?v=COmxq7bh-fM",
            "title": "ARK video",
            "channel": "ARK Invest",
        }
        args = mock.Mock(
            min_video_seconds=30.0,
            search_results=5,
            yt_dlp_js_runtime="node",
            yt_dlp_pot_provider_url="",
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(process_ark_videos, "resolve_ark_source", return_value=source),
                mock.patch.object(
                    process_ark_videos,
                    "download_selected_source",
                    side_effect=[RuntimeError("Wistia failed"), None],
                ) as download,
                mock.patch.object(process_ark_videos, "resolve_youtube_url", return_value=youtube),
                mock.patch.object(process_ark_videos, "validate_downloaded_source", return_value=600.0),
            ):
                selected, duration = process_ark_videos.acquire_ark_source(
                    {"download_query": "ARK video", "title": "ARK video"},
                    root / "source.mp4",
                    root,
                    args,
                )

        self.assertEqual(download.call_count, 2)
        self.assertEqual(selected["provider"], "youtube")
        self.assertEqual(duration, 600.0)

    def test_write_wistia_transcript_converts_timed_vtt(self) -> None:
        vtt = """WEBVTT

00:00:00.000 --> 00:00:02.800
On this episode of In the Know,

00:00:02.800 --> 00:00:06.960
we talk about the jobs report.
"""
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.json"
            with mock.patch.object(process_ark_videos, "fetch_text_direct", return_value=vtt):
                process_ark_videos.write_wistia_transcript(
                    {"caption_url": "https://fast.wistia.net/embed/captions/test.vtt?language=eng"},
                    transcript,
                )
            payload = json.loads(transcript.read_text(encoding="utf-8"))

        self.assertEqual(payload["model"], "wistia-captions")
        self.assertEqual(payload["duration"], 6.96)
        self.assertEqual(len(payload["segments"]), 2)
        self.assertEqual(payload["segments"][1]["start"], 2.8)


if __name__ == "__main__":
    unittest.main()

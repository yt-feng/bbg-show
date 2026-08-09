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
                    "discover_ark_wistia_candidates",
                    return_value=[{"media_id": "footer1234", "provenance": "test"}],
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
                    "discover_ark_wistia_candidates",
                    return_value=[
                        {"media_id": "candidate1", "provenance": "test"},
                        {"media_id": "candidate2", "provenance": "test"},
                    ],
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

    def test_exact_ledger_entry_bypasses_live_page_discovery(self) -> None:
        page_url = (
            "https://www.ark-invest.com/videos/market-commentary/"
            "august-2026-in-the-know-cathie-wood"
        )
        item = {
            "url": page_url,
            "guid": page_url,
            "slug": "august_2026_in_the_know_cathie_wood",
            "title": 'Why This "Scary" Jobs Report Might Be Good News | ITK With Cathie Wood',
            "pub_date": "2026-08-08T09:00:00+08:00",
        }
        media = {
            "hashedId": "s9r5knrzoe",
            "name": "ITK August",
            "seoDescription": "an In The Know video",
            "createdAt": 1786152080,
            "duration": 3690.77,
            "protected": False,
            "hls_enabled": True,
            "assets": [{
                "type": "hd_mp4_video",
                "height": 720,
                "public": True,
                "status": 2,
                "url": "https://embed-ssl.wistia.com/deliveries/source.bin",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "wistia_sources.json"
            ledger.write_text(json.dumps({
                "schema_version": 1,
                "sources": [{
                    **item,
                    "wistia_id": "s9r5knrzoe",
                }],
            }), encoding="utf-8")
            with (
                mock.patch.object(
                    process_ark_videos,
                    "discover_ark_wistia_candidates",
                ) as discover,
                mock.patch.object(
                    process_ark_videos,
                    "fetch_wistia_media",
                    return_value=(media, json.dumps({"media": media}), "https://metadata"),
                ),
            ):
                source = process_ark_videos.resolve_wistia_source(
                    item,
                    root,
                    max_height=720,
                    source_ledger=ledger,
                    tavily_api_key="unused",
                )

        discover.assert_not_called()
        self.assertEqual(source["media_id"], "s9r5knrzoe")
        self.assertEqual(source["provenance"], "ledger")

    def test_ledger_integrity_mismatch_continues_live_discovery(self) -> None:
        page_url = "https://www.ark-invest.com/videos/market-commentary/august-itk"
        item = {
            "url": page_url,
            "slug": "august_itk",
            "title": "August In The Know Jobs Update",
            "pub_date": "2026-08-08T09:00:00+08:00",
        }
        media = {
            "hashedId": "s9r5knrzoe",
            "name": "August ITK Jobs Update",
            "createdAt": 1786152080,
            "duration": 600,
            "protected": False,
            "hls_enabled": True,
            "assets": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "wistia_sources.json"
            ledger.write_text(json.dumps({
                "schema_version": 1,
                "sources": [{
                    "url": page_url,
                    "slug": "august_itk",
                    "title": "Wrong cached title",
                    "pub_date": item["pub_date"],
                    "wistia_id": "wrong12345",
                }],
            }), encoding="utf-8")
            with (
                mock.patch.object(
                    process_ark_videos,
                    "discover_ark_wistia_candidates",
                    return_value=[{"media_id": "s9r5knrzoe", "provenance": "tavily_extract"}],
                ),
                mock.patch.object(
                    process_ark_videos,
                    "fetch_wistia_media",
                    return_value=(media, json.dumps({"media": media}), "https://metadata"),
                ),
            ):
                source = process_ark_videos.resolve_wistia_source(
                    item,
                    root,
                    source_ledger=ledger,
                    tavily_api_key="test-key",
                )

        self.assertEqual(source["media_id"], "s9r5knrzoe")
        self.assertEqual(source["provenance"], "tavily_extract")

    def test_conflicting_ledger_mappings_are_rejected(self) -> None:
        first_url = "https://www.ark-invest.com/videos/market-commentary/first-item"
        second_url = "https://www.ark-invest.com/videos/market-commentary/second-item"
        first_entry = {
            "url": first_url,
            "slug": "first_item",
            "title": "First Item",
            "pub_date": "2026-08-08T09:00:00+08:00",
            "wistia_id": "s9r5knrzoe",
        }
        conflict_sets = [
            [
                first_entry,
                {
                    "url": second_url,
                    "slug": "second_item",
                    "title": "Second Item",
                    "pub_date": "2026-08-09T09:00:00+08:00",
                    "wistia_id": "s9r5knrzoe",
                },
            ],
            [
                first_entry,
                {
                    **first_entry,
                    "wistia_id": "wrong12345",
                },
            ],
        ]
        for sources in conflict_sets:
            for ordered_sources in (sources, list(reversed(sources))):
                with self.subTest(sources=ordered_sources):
                    with tempfile.TemporaryDirectory() as tmp:
                        ledger = Path(tmp) / "wistia_sources.json"
                        ledger.write_text(json.dumps({
                            "schema_version": 1,
                            "sources": ordered_sources,
                        }), encoding="utf-8")
                        candidates = process_ark_videos.ledger_wistia_candidates(
                            {
                                "url": first_url,
                                "slug": "first_item",
                                "title": "First Item",
                                "pub_date": "2026-08-08T09:00:00+08:00",
                            },
                            ledger,
                        )

                    self.assertEqual(candidates, [])

    def test_tavily_exact_page_extract_request_and_response(self) -> None:
        page_url = "https://www.ark-invest.com/videos/market-commentary/august-itk"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps({
            "results": [{
                "url": page_url + "/",
                "raw_content": "https://fast.wistia.com/embed/medias/s9r5knrzoe.m3u8",
            }],
            "failed_results": [],
            "request_id": "request-123",
            "usage": {"credits": 2},
        }).encode("utf-8")
        with mock.patch.object(process_ark_videos.urllib.request, "urlopen", return_value=response) as urlopen:
            content = process_ark_videos.fetch_ark_page_with_tavily(
                page_url,
                "tvly-test-key",
            )

        request = urlopen.call_args.args[0]
        request_payload = json.loads(request.data)
        self.assertEqual(request.full_url, process_ark_videos.TAVILY_EXTRACT_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer tvly-test-key")
        self.assertEqual(request_payload["urls"], page_url)
        self.assertEqual(request_payload["extract_depth"], "advanced")
        self.assertIn("s9r5knrzoe", content)

    def test_tavily_http_error_does_not_echo_response_body(self) -> None:
        page_url = "https://www.ark-invest.com/videos/market-commentary/august-itk"
        error = process_ark_videos.urllib.error.HTTPError(
            process_ark_videos.TAVILY_EXTRACT_URL,
            401,
            "Unauthorized",
            {},
            None,
        )
        error.read = mock.Mock(return_value=b"server echoed tvly-test-key")
        with mock.patch.object(
            process_ark_videos.urllib.request,
            "urlopen",
            side_effect=error,
        ):
            with self.assertRaisesRegex(RuntimeError, "Tavily Extract HTTP 401") as raised:
                process_ark_videos.fetch_ark_page_with_tavily(page_url, "tvly-test-key")

        self.assertNotIn("tvly-test-key", str(raised.exception))
        error.read.assert_not_called()

    def test_tavily_discovery_avoids_headless_after_ark_and_reader_403(self) -> None:
        page_url = "https://www.ark-invest.com/videos/market-commentary/august-itk"
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(
                    process_ark_videos,
                    "fetch_text_direct",
                    side_effect=process_ark_videos.FetchError("HTTP 403"),
                ),
                mock.patch.object(
                    process_ark_videos,
                    "fetch_ark_page_with_tavily",
                    return_value="https://fast.wistia.com/embed/medias/s9r5knrzoe.m3u8",
                ),
                mock.patch.object(
                    process_ark_videos,
                    "fetch_ark_page_with_headless_chrome",
                ) as headless,
            ):
                candidates = process_ark_videos.discover_ark_wistia_candidates(
                    page_url,
                    Path(tmp),
                    tavily_api_key="test-key",
                )

        headless.assert_not_called()
        self.assertEqual(candidates, [{"media_id": "s9r5knrzoe", "provenance": "tavily_extract"}])

    def test_source_ledger_updates_only_verified_wistia_successes_idempotently(self) -> None:
        success = {
            "status": "success",
            "media_provider": "wistia",
            "url": "https://www.ark-invest.com/videos/market-commentary/august-itk",
            "guid": "https://www.ark-invest.com/videos/market-commentary/august-itk",
            "slug": "august_itk",
            "source_title": "August In The Know Jobs Update",
            "pub_date": "2026-08-08T09:00:00+08:00",
            "wistia_id": "s9r5knrzoe",
            "wistia_media_name": "ITK August",
            "wistia_media_created_at": 1786152080,
            "wistia_provenance": "tavily_extract",
        }
        with tempfile.TemporaryDirectory() as tmp:
            ledger = Path(tmp) / "wistia_sources.json"
            process_ark_videos.update_wistia_source_ledger(ledger, [success])
            first = ledger.read_bytes()
            process_ark_videos.update_wistia_source_ledger(ledger, [success])
            second = ledger.read_bytes()
            process_ark_videos.update_wistia_source_ledger(
                ledger,
                [{**success, "media_provider": "youtube", "wistia_id": ""}],
            )
            payload = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(first, second)
        self.assertEqual(len(payload["sources"]), 1)
        self.assertEqual(payload["sources"][0]["wistia_id"], "s9r5knrzoe")

    def test_source_ledger_writer_preserves_malformed_and_conflicting_files(self) -> None:
        success = {
            "status": "success",
            "media_provider": "wistia",
            "url": "https://www.ark-invest.com/videos/market-commentary/new-item",
            "slug": "new_item",
            "source_title": "New Item",
            "pub_date": "2026-08-10T09:00:00+08:00",
            "wistia_id": "newid12345",
        }
        first_url = "https://www.ark-invest.com/videos/market-commentary/first-item"
        second_url = "https://www.ark-invest.com/videos/market-commentary/second-item"
        conflicting = json.dumps({
            "schema_version": 1,
            "sources": [
                {"url": first_url, "wistia_id": "s9r5knrzoe"},
                {"url": second_url, "wistia_id": "s9r5knrzoe"},
            ],
        }).encode("utf-8")
        for original in (b"{broken json", conflicting):
            with self.subTest(original=original):
                with tempfile.TemporaryDirectory() as tmp:
                    ledger = Path(tmp) / "wistia_sources.json"
                    ledger.write_bytes(original)
                    process_ark_videos.update_wistia_source_ledger(ledger, [success])
                    preserved = ledger.read_bytes()

                self.assertEqual(preserved, original)

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

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import download_bloomberg_video as downloader  # noqa: E402
import collect_top_video_outputs as collector  # noqa: E402
import process_top_videos as processor  # noqa: E402
import scrape_top_videos as scraper  # noqa: E402


def youtube_feed(entries: list[dict[str, str]], *, channel_id: str = scraper.YOUTUBE_CHANNEL_ID) -> bytes:
    rows = []
    for item in entries:
        entry_url = item.get("url", f"https://www.youtube.com/watch?v={item['video_id']}")
        rows.append(
            f"""
  <entry>
    <id>yt:video:{item['video_id']}</id>
    <yt:videoId>{item['video_id']}</yt:videoId>
    <yt:channelId>{item.get('channel_id', scraper.YOUTUBE_CHANNEL_ID)}</yt:channelId>
    <title>{item['title']}</title>
    <link rel="alternate" href="{entry_url}"/>
    <published>{item.get('published', '2026-07-11T20:00:00+00:00')}</published>
    <media:group><media:description>{item.get('description', '')}</media:description></media:group>
  </entry>
"""
        )
    return (
        f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{scraper.ATOM_NS}" xmlns:yt="{scraper.YOUTUBE_NS}" xmlns:media="{scraper.MEDIA_NS}">
  <yt:channelId>{channel_id}</yt:channelId>
  <link rel="alternate" href="https://www.youtube.com/channel/{scraper.YOUTUBE_CHANNEL_ID}"/>
  {''.join(rows)}
</feed>
"""
    ).encode()


class YouTubeFeedParserTests(unittest.TestCase):
    def test_scans_full_feed_and_keeps_three_fresh_eligible_unique_videos(self) -> None:
        entries = [
            {
                "video_id": "SHORTS00001",
                "title": "A market thought #shorts",
                "url": "https://www.youtube.com/shorts/SHORTS00001",
            },
            {"video_id": "SENSITIV001", "title": "Ukraine Seeks Faster Patriot Production"},
            {
                "video_id": "EPISODE0001",
                "title": "Bloomberg This Weekend | Full Show",
                "description": "00:00 - Intro\n10:00 - News\n20:00 - Interview",
            },
            {"video_id": "DUPTITLE001", "title": "Apple OpenAI Rift Deepens Over AI Talent"},
            {"video_id": "QUIZVIDEO01", "title": "Pointed News Quiz | ITALY, REPTILES, BEAUTY"},
            {
                "video_id": "OLDVIDEO001",
                "title": "An old but otherwise useful business interview",
                "published": "2026-07-08T12:00:00+00:00",
            },
            {"video_id": "ELIGIBLE001", "title": "Lumen Completes Acquisition of Cloud Networking Firm"},
            {"video_id": "ELIGIBLE002", "title": "Retailers Rework Supply Chains as Costs Shift"},
            {"video_id": "ELIGIBLE003", "title": "Chip Demand Lifts Data Center Forecasts"},
        ]
        existing = {scraper.normalize_title_key("Duration: 2:43 Apple OpenAI Rift Deepens Over AI Talent")}

        videos = scraper.parse_youtube_feed(
            youtube_feed(entries),
            max_videos=3,
            existing_title_keys=existing,
            now=datetime(2026, 7, 12, tzinfo=timezone.utc),
            max_age_hours=48,
        )

        self.assertEqual(
            {item["youtube_id"] for item in videos},
            {"ELIGIBLE001", "ELIGIBLE002", "ELIGIBLE003"},
        )
        for item in videos:
            self.assertEqual(item["source"], "youtube-backup")
            self.assertEqual(item["channel_id"], scraper.YOUTUBE_CHANNEL_ID)
            self.assertEqual(item["url"], f"https://www.youtube.com/watch?v={item['youtube_id']}")
            self.assertIn(item["youtube_id"].lower(), item["slug"])

    def test_rejects_feed_that_is_not_the_official_channel(self) -> None:
        with self.assertRaisesRegex(ValueError, "official Bloomberg channel"):
            scraper.parse_youtube_feed(
                youtube_feed([], channel_id="UC_NOT_BLOOMBERG"),
                max_videos=3,
                now=datetime(2026, 7, 12, tzinfo=timezone.utc),
            )

    def test_accepts_youtube_feed_level_channel_id_without_uc_prefix(self) -> None:
        videos = scraper.parse_youtube_feed(
            youtube_feed(
                [{"video_id": "ELIGIBLE001", "title": "Cloud Networking Acquisition"}],
                channel_id=scraper.YOUTUBE_CHANNEL_ID.removeprefix("UC"),
            ),
            max_videos=1,
            now=datetime(2026, 7, 12, tzinfo=timezone.utc),
        )
        self.assertEqual([item["youtube_id"] for item in videos], ["ELIGIBLE001"])

    def test_main_appends_backup_without_real_network(self) -> None:
        primary = {
            "url": "https://www.bloomberg.com/news/videos/2026-07-11/example-video",
            "title": "Duration: 2:00 Primary Bloomberg Video",
            "slug": "primary_bloomberg_video",
            "source": "bloomberg",
        }
        backup = {
            "url": "https://www.youtube.com/watch?v=ELIGIBLE001",
            "title": "YouTube Backup Video",
            "slug": "youtube_eligible001_youtube_backup_video",
            "source": "youtube-backup",
            "youtube_id": "ELIGIBLE001",
            "channel_id": scraper.YOUTUBE_CHANNEL_ID,
            "published_at": "2026-07-11T20:00:00+00:00",
            "description": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "manifest.json"
            argv = [
                "scrape_top_videos.py",
                "--out",
                str(output),
                "--youtube-backup-videos",
                "1",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(scraper, "scrape", return_value=("direct", [primary])),
                mock.patch.object(scraper, "fetch_youtube_feed", return_value=b"feed") as fetch,
                mock.patch.object(scraper, "parse_youtube_feed", return_value=[backup]) as parse,
            ):
                scraper.main()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["primary_count"], 1)
        self.assertEqual(payload["youtube_backup_count"], 1)
        self.assertEqual(payload["count"], 2)
        fetch.assert_called_once()
        self.assertEqual(parse.call_args.kwargs["max_videos"], 1)

    def test_main_does_not_fetch_youtube_when_backup_defaults_to_zero(self) -> None:
        primary = {
            "url": "https://www.bloomberg.com/news/videos/2026-07-11/example-video",
            "title": "Primary Bloomberg Video",
            "slug": "primary_bloomberg_video",
            "source": "bloomberg",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "manifest.json"
            with (
                mock.patch.object(sys, "argv", ["scrape_top_videos.py", "--out", str(output)]),
                mock.patch.object(scraper, "scrape", return_value=("direct", [primary])),
                mock.patch.object(scraper, "fetch_youtube_feed") as fetch,
            ):
                scraper.main()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["youtube_backup_requested"], 0)
        fetch.assert_not_called()

    def test_primary_scrape_failure_can_use_youtube_only(self) -> None:
        backup = {
            "url": "https://www.youtube.com/watch?v=ELIGIBLE001",
            "title": "YouTube Backup Video",
            "slug": "youtube_eligible001_youtube_backup_video",
            "source": "youtube-backup",
            "youtube_id": "ELIGIBLE001",
            "channel_id": scraper.YOUTUBE_CHANNEL_ID,
            "published_at": "2026-07-11T20:00:00+00:00",
            "description": "",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "manifest.json"
            argv = [
                "scrape_top_videos.py",
                "--out",
                str(output),
                "--youtube-backup-videos",
                "1",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(scraper, "scrape", side_effect=SystemExit("primary unavailable")),
                mock.patch.object(scraper, "fetch_youtube_feed", return_value=b"feed"),
                mock.patch.object(scraper, "parse_youtube_feed", return_value=[backup]),
            ):
                scraper.main()
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["scrape_method"], "youtube-backup-only")
        self.assertEqual(payload["primary_count"], 0)
        self.assertEqual(payload["count"], 1)

    def test_reachable_sources_with_no_eligible_videos_write_empty_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "manifest.json"
            argv = [
                "scrape_top_videos.py",
                "--out",
                str(output),
                "--youtube-backup-videos",
                "3",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(scraper, "scrape", side_effect=SystemExit("primary unavailable")),
                mock.patch.object(scraper, "fetch_youtube_feed", return_value=b"feed"),
                mock.patch.object(scraper, "parse_youtube_feed", return_value=[]),
            ):
                scraper.main()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(payload["selection_status"], "no_eligible_videos")
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["videos"], [])
        self.assertEqual(payload["primary_error"], "primary unavailable")


class YouTubeDownloaderTests(unittest.TestCase):
    def test_youtube_id_and_slug_cover_watch_short_and_youtu_be_urls(self) -> None:
        urls = [
            "https://www.youtube.com/watch?v=HQDWoxHF62M",
            "https://youtube.com/shorts/HQDWoxHF62M",
            "https://youtu.be/HQDWoxHF62M?t=10",
        ]
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(downloader.youtube_video_id(url), "HQDWoxHF62M")
                self.assertTrue(downloader.is_youtube_url(url))
                self.assertEqual(downloader.slug_from_url(url), "youtube_hqdwoxhf62m")

    def test_direct_youtube_command_uses_node_and_never_adds_proxy(self) -> None:
        args = argparse.Namespace(
            yt_dlp_bin=None,
            workers=32,
            url="https://www.youtube.com/watch?v=HQDWoxHF62M",
            google_doh=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "source.mp4"
            with (
                mock.patch.object(downloader, "yt_dlp_base_command", return_value=["yt-dlp"]),
                mock.patch.object(downloader, "run_ytdlp_process", return_value=True) as run,
            ):
                self.assertTrue(downloader.run_ytdlp_page_downloader(args, output))

        command = run.call_args.args[0]
        self.assertIn("--js-runtimes", command)
        self.assertEqual(command[command.index("--js-runtimes") + 1], "node")
        self.assertEqual(command[command.index("--concurrent-fragments") + 1], "8")
        self.assertIn("--no-playlist", command)
        self.assertNotIn("--proxy", command)
        self.assertEqual(command[-1], args.url)

    def test_proxied_youtube_command_hides_upstream_credentials(self) -> None:
        args = argparse.Namespace(
            yt_dlp_bin=None,
            workers=32,
            url="https://www.youtube.com/watch?v=HQDWoxHF62M",
            google_doh=True,
        )
        upstream = "https://user:secret@example.com:443"
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "source.mp4"
            with (
                mock.patch.object(downloader, "yt_dlp_base_command", return_value=["yt-dlp"]),
                mock.patch.object(downloader, "LocalProxyServer") as forwarder,
                mock.patch.object(downloader, "run_ytdlp_process", return_value=True) as run,
            ):
                forwarder.return_value.__enter__.return_value = "http://127.0.0.1:43123"
                self.assertTrue(
                    downloader.run_ytdlp_page_downloader(args, output, proxy=upstream)
                )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--proxy") + 1], "http://127.0.0.1:43123")
        self.assertNotIn(upstream, command)
        forwarder.assert_called_once_with(upstream, google_doh=True)

    def test_youtube_proxy_candidates_are_http_only_deduplicated_and_limited(self) -> None:
        nodes = [
            "socks5://socks.example:1080",
            "https://user:pass@one.example:443#first",
            "https://user:pass@one.example:443#duplicate",
            "http://two.example:8080",
            "vmess://unsupported",
            "https://three.example:443",
        ]
        with mock.patch.object(downloader.hls_downloader, "load_subscription", return_value=nodes):
            candidates = downloader.youtube_proxy_candidates(Path("subscription.txt"), limit=2)

        self.assertEqual(
            candidates,
            ["https://user:pass@one.example:443", "http://two.example:8080"],
        )

    def test_youtube_proxy_fallback_stops_after_first_success_and_caps_attempts(self) -> None:
        args = argparse.Namespace()
        proxies = [
            "https://one.example:443",
            "https://two.example:443",
            "https://three.example:443",
            "https://four.example:443",
        ]
        with (
            mock.patch.object(
                downloader,
                "youtube_proxy_candidates",
                return_value=proxies[: downloader.YOUTUBE_PROXY_ATTEMPT_LIMIT],
            ) as candidates,
            mock.patch.object(
                downloader,
                "run_ytdlp_page_downloader",
                side_effect=[False, True],
            ) as run,
        ):
            self.assertTrue(
                downloader.run_ytdlp_page_downloader_with_proxies(
                    args,
                    Path("subscription.txt"),
                    Path("output.mp4"),
                )
            )

        candidates.assert_called_once_with(Path("subscription.txt"))
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].kwargs["proxy"], proxies[0])
        self.assertEqual(run.call_args_list[1].kwargs["proxy"], proxies[1])

    def test_failed_ytdlp_attempt_removes_exact_partial_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "partial.mp4"
            output.write_bytes(b"incomplete")
            with mock.patch.object(
                downloader.subprocess,
                "run",
                return_value=downloader.subprocess.CompletedProcess(["yt-dlp"], 1),
            ):
                self.assertFalse(
                    downloader.run_ytdlp_process(["yt-dlp", "video"], output=output)
                )

            self.assertFalse(output.exists())

    def test_main_routes_youtube_before_bloomberg_asset_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "youtube.mp4"
            work_dir = root / "download"
            argv = [
                "download_bloomberg_video.py",
                "--url",
                "https://www.youtube.com/watch?v=HQDWoxHF62M",
                "--output",
                str(output),
                "--work-dir",
                str(work_dir),
                "--no-strategy-cache",
                "--keep-tmp",
            ]
            with (
                mock.patch.object(downloader, "run_ytdlp_page_downloader", return_value=True) as run,
                mock.patch.object(downloader, "ensure_subscription") as subscription,
                mock.patch.object(downloader, "verify_output") as verify,
                mock.patch.object(
                    downloader,
                    "cached_asset_id_for_url",
                    side_effect=AssertionError("Bloomberg discovery must not run for YouTube"),
                ),
            ):
                self.assertEqual(downloader.main(argv[1:]), 0)

            plan = json.loads((work_dir / "download_plan.json").read_text(encoding="utf-8"))

        self.assertEqual(plan["youtube_id"], "HQDWoxHF62M")
        self.assertEqual(plan["source_kind"], "youtube-page")
        run.assert_called_once()
        subscription.assert_not_called()
        verify.assert_called_once_with(output)

    def test_main_retries_youtube_through_subscription_proxy_after_direct_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "youtube.mp4"
            work_dir = root / "download"
            subscription_path = root / "subscription.txt"
            argv = [
                "download_bloomberg_video.py",
                "--url",
                "https://www.youtube.com/watch?v=HQDWoxHF62M",
                "--output",
                str(output),
                "--work-dir",
                str(work_dir),
                "--no-strategy-cache",
                "--keep-tmp",
            ]
            with (
                mock.patch.object(
                    downloader,
                    "run_ytdlp_page_downloader",
                    side_effect=[False, True],
                ) as run,
                mock.patch.object(downloader, "ensure_subscription", return_value=subscription_path) as ensure,
                mock.patch.object(
                    downloader,
                    "youtube_proxy_candidates",
                    return_value=["https://user:secret@proxy.example:443"],
                ),
                mock.patch.object(downloader, "verify_output") as verify,
            ):
                self.assertEqual(downloader.main(argv[1:]), 0)

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args, (mock.ANY, output))
        self.assertNotIn("proxy", run.call_args_list[0].kwargs)
        self.assertEqual(
            run.call_args_list[1].kwargs["proxy"],
            "https://user:secret@proxy.example:443",
        )
        ensure.assert_called_once()
        verify.assert_called_once_with(output)

    def test_youtube_proxy_modes_never_and_always_control_direct_attempts(self) -> None:
        base_argv = [
            "download_bloomberg_video.py",
            "--url",
            "https://www.youtube.com/watch?v=HQDWoxHF62M",
            "--no-strategy-cache",
            "--keep-tmp",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.object(downloader, "run_ytdlp_page_downloader", return_value=False) as direct,
                mock.patch.object(downloader, "ensure_subscription") as ensure,
                self.assertRaisesRegex(SystemExit, "YouTube page download failed"),
            ):
                downloader.main(
                    [
                        *base_argv[1:],
                        "--output",
                        str(root / "never.mp4"),
                        "--work-dir",
                        str(root / "never-work"),
                        "--yt-dlp-proxy-mode",
                        "never",
                    ]
                )
            direct.assert_called_once()
            ensure.assert_not_called()

            subscription_path = root / "subscription.txt"
            with (
                mock.patch.object(downloader, "run_ytdlp_page_downloader") as direct,
                mock.patch.object(
                    downloader,
                    "ensure_subscription",
                    return_value=subscription_path,
                ) as ensure,
                mock.patch.object(
                    downloader,
                    "run_ytdlp_page_downloader_with_proxies",
                    return_value=True,
                ) as proxied,
                mock.patch.object(downloader, "verify_output"),
            ):
                self.assertEqual(
                    downloader.main(
                        [
                            *base_argv[1:],
                            "--output",
                            str(root / "always.mp4"),
                            "--work-dir",
                            str(root / "always-work"),
                            "--yt-dlp-proxy-mode",
                            "always",
                        ]
                    ),
                    0,
                )
            direct.assert_not_called()
            ensure.assert_called_once()
            proxied.assert_called_once_with(mock.ANY, subscription_path, root / "always.mp4")


class TopVideoManifestTests(unittest.TestCase):
    def test_title_refinement_technical_failure_keeps_original_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            original = '{"clips": [{"title": "planner title"}]}\n'
            plan_path.write_text(original, encoding="utf-8")
            error = processor.subprocess.CalledProcessError(1, ["refiner"])
            with mock.patch.object(processor, "run", side_effect=error):
                refined = processor.refine_title_or_keep_planner_title(plan_path)

            self.assertFalse(refined)
            self.assertEqual(plan_path.read_text(encoding="utf-8"), original)

    def test_title_refinement_sensitive_failure_is_not_swallowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.json"
            plan_path.write_text('{"clips": []}', encoding="utf-8")
            with (
                mock.patch.object(
                    processor,
                    "run",
                    side_effect=RuntimeError("Top video skipped by sensitive topic filter"),
                ),
                self.assertRaisesRegex(RuntimeError, "sensitive topic filter"),
            ):
                processor.refine_title_or_keep_planner_title(plan_path)

    def test_run_date_validation_rejects_path_traversal(self) -> None:
        self.assertEqual(processor.validate_run_date("2026-07-12"), "2026-07-12")
        for value in ("..", "2026-7-12", "2026-07-12/../..", "2026-07-12\nother=value"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "strict YYYY-MM-DD"):
                    processor.validate_run_date(value)

    def test_partial_success_is_publishable_regardless_of_failure_rate(self) -> None:
        result = collector.evaluate_batch_summary(
            {"total": 8, "succeeded": 1, "failed": 7, "skipped": 0}
        )

        self.assertEqual(result["outcome"], "partial_success")
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failure_rate"], 7 / 8)

    def test_batch_fails_only_when_all_processable_candidates_fail(self) -> None:
        with self.assertRaisesRegex(collector.BatchEvaluationError, "No top videos"):
            collector.evaluate_batch_summary(
                {"total": 4, "succeeded": 0, "failed": 4, "skipped": 0}
            )

        skipped = collector.evaluate_batch_summary(
            {"total": 4, "succeeded": 0, "failed": 0, "skipped": 4}
        )
        self.assertEqual(skipped["outcome"], "no_processable_candidates")

    def test_load_manifest_preserves_backup_metadata_and_stable_order(self) -> None:
        payload = {
            "videos": [
                {
                    "url": "https://www.youtube.com/watch?v=SENSITIV001",
                    "title": "Ukraine Defense Update",
                    "slug": "youtube_sensitive001",
                    "source": "youtube-backup",
                    "youtube_id": "SENSITIV001",
                    "description": "Sensitive item retained for stable matrix indexing.",
                },
                {
                    "url": "https://www.youtube.com/watch?v=ELIGIBLE001",
                    "title": "Cloud Networking Acquisition",
                    "slug": "youtube_eligible001",
                    "source": "youtube-backup",
                    "youtube_id": "ELIGIBLE001",
                },
            ]
        }
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "manifest.json"
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            videos = processor.load_manifest(manifest, 12)

        self.assertEqual([item["youtube_id"] for item in videos], ["SENSITIV001", "ELIGIBLE001"])
        self.assertTrue(all(item["source"] == "youtube-backup" for item in videos))

    def test_failed_render_cleanup_removes_partial_directory(self) -> None:
        item = {
            "url": "https://www.youtube.com/watch?v=ELIGIBLE001",
            "slug": "youtube_eligible001",
        }
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            render_dir = processor.render_output_dir(item, 2, output_dir)
            render_dir.mkdir()
            (render_dir / "partial.mp4").write_bytes(b"partial")

            processor.cleanup_failed_render_output(item, 2, output_dir)

            self.assertFalse(render_dir.exists())

    def test_collect_prunes_failed_and_undeclared_mp4_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            success_dir = output_dir / "01_success"
            failed_dir = output_dir / "02_failed"
            success_dir.mkdir()
            failed_dir.mkdir()
            (success_dir / "good.mp4").write_bytes(b"complete")
            (success_dir / "partial.mp4").write_bytes(b"partial")
            (failed_dir / "broken.mp4").write_bytes(b"broken")
            (output_dir / "summary_01.json").write_text(
                json.dumps(
                    {
                        "total": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "skipped": 0,
                        "videos": [
                            {
                                "status": "success",
                                "index": 1,
                                "output_dir": "rendered-clips/top-videos/2026-07-12/01_success",
                                "rendered_files": ["good.mp4"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (output_dir / "summary_02.json").write_text(
                json.dumps(
                    {
                        "total": 1,
                        "succeeded": 0,
                        "failed": 1,
                        "skipped": 0,
                        "videos": [{"status": "failed", "index": 2}],
                    }
                ),
                encoding="utf-8",
            )

            result = collector.prune_unsuccessful_outputs(output_dir)

            self.assertTrue((success_dir / "good.mp4").exists())
            self.assertFalse((success_dir / "partial.mp4").exists())
            self.assertFalse(failed_dir.exists())
            self.assertEqual(result["allowed_dirs"], 1)

    def test_malformed_summary_becomes_indexed_failure_without_discarding_other_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            success_dir = output_dir / "01_success"
            partial_dir = output_dir / "02_partial"
            success_dir.mkdir()
            partial_dir.mkdir()
            (success_dir / "good.mp4").write_bytes(b"complete")
            (partial_dir / "partial.mp4").write_bytes(b"partial")
            (output_dir / "summary_01.json").write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "status": "success",
                                "index": 1,
                                "output_dir": "01_success",
                                "rendered_files": ["good.mp4"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            malformed_path = output_dir / "summary_02.json"
            malformed_path.write_text('{"videos": [', encoding="utf-8")

            result = collector.prune_unsuccessful_outputs(output_dir)
            replacement = json.loads(malformed_path.read_text(encoding="utf-8"))

            self.assertTrue((success_dir / "good.mp4").exists())
            self.assertFalse(partial_dir.exists())
            self.assertEqual(result["allowed_dirs"], 1)
            self.assertEqual(result["invalid_summaries"], 1)
            self.assertEqual(replacement["failed"], 1)
            self.assertEqual(replacement["videos"][0]["status"], "failed")
            self.assertEqual(replacement["videos"][0]["index"], 2)

    def test_non_object_result_is_ignored_without_losing_valid_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            success_dir = output_dir / "01_success"
            success_dir.mkdir()
            (success_dir / "good.mp4").write_bytes(b"complete")
            summary_path = output_dir / "summary_01.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "status": "success",
                                "index": 1,
                                "output_dir": "01_success",
                                "rendered_files": ["good.mp4"],
                            },
                            "not-an-object",
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = collector.prune_unsuccessful_outputs(output_dir)
            normalized = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertEqual(result["invalid_items"], 1)
            self.assertEqual(result["allowed_dirs"], 1)
            self.assertEqual(normalized["total"], 1)
            self.assertEqual(normalized["succeeded"], 1)
            self.assertEqual(len(normalized["videos"]), 1)
            self.assertIsInstance(normalized["videos"][0], dict)

    def test_success_cannot_claim_an_output_directory_from_another_index(self) -> None:
        cases = (
            (1, "02_claimed", "result index"),
            (2, "01_claimed", "declared output directory"),
        )
        for item_index, directory_name, expected_error in cases:
            with self.subTest(item_index=item_index, directory_name=directory_name):
                with tempfile.TemporaryDirectory() as tmp:
                    output_dir = Path(tmp)
                    claimed_dir = output_dir / directory_name
                    claimed_dir.mkdir()
                    (claimed_dir / "clip.mp4").write_bytes(b"complete")
                    summary_path = output_dir / "summary_02.json"
                    summary_path.write_text(
                        json.dumps(
                            {
                                "videos": [
                                    {
                                        "status": "success",
                                        "index": item_index,
                                        "output_dir": directory_name,
                                        "rendered_files": ["clip.mp4"],
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )

                    result = collector.prune_unsuccessful_outputs(output_dir)
                    normalized = json.loads(summary_path.read_text(encoding="utf-8"))

                    self.assertFalse(claimed_dir.exists())
                    self.assertEqual(result["allowed_dirs"], 0)
                    self.assertEqual(result["invalid_successes"], 1)
                    self.assertEqual(normalized["videos"][0]["status"], "failed")
                    self.assertEqual(normalized["videos"][0]["index"], 2)
                    self.assertIn(expected_error, normalized["videos"][0]["error"])

    def test_summary_index_cannot_exceed_expected_manifest_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            unexpected_dir = output_dir / "02_unexpected"
            unexpected_dir.mkdir()
            (unexpected_dir / "clip.mp4").write_bytes(b"complete")
            summary_path = output_dir / "summary_02.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "status": "success",
                                "index": 2,
                                "output_dir": "02_unexpected",
                                "rendered_files": ["clip.mp4"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            result = collector.prune_unsuccessful_outputs(output_dir, expected_total=1)

            self.assertFalse(summary_path.exists())
            self.assertFalse(unexpected_dir.exists())
            self.assertEqual(result["allowed_dirs"], 0)
            self.assertEqual(result["invalid_summaries"], 1)

    def test_all_skipped_rerun_restores_previously_published_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "current"
            previous_dir = root / "previous"
            output_dir.mkdir()
            previous_dir.mkdir()
            (output_dir / "summary.json").write_text(
                json.dumps({"total": 1, "succeeded": 0, "failed": 0, "skipped": 1}),
                encoding="utf-8",
            )
            (output_dir / "top_videos.json").write_text("new", encoding="utf-8")
            (previous_dir / "summary.json").write_text(
                json.dumps({"total": 1, "succeeded": 1, "failed": 0, "skipped": 0}),
                encoding="utf-8",
            )
            published = previous_dir / "01_published"
            published.mkdir()
            (published / "clip.mp4").write_bytes(b"published")

            restored = collector.restore_previous_if_no_success(output_dir, previous_dir)

            self.assertTrue(restored)
            self.assertTrue((output_dir / "01_published" / "clip.mp4").exists())
            self.assertFalse((output_dir / "top_videos.json").exists())

    def test_empty_declared_success_is_converted_to_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            render_dir = output_dir / "01_empty"
            render_dir.mkdir()
            (render_dir / "empty.mp4").touch()
            summary_path = output_dir / "summary_01.json"
            summary_path.write_text(
                json.dumps(
                    {
                        "total": 1,
                        "succeeded": 1,
                        "failed": 0,
                        "skipped": 0,
                        "videos": [
                            {
                                "status": "success",
                                "index": 1,
                                "output_dir": "rendered-clips/top-videos/2026-07-12/01_empty",
                                "rendered_files": ["empty.mp4"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            result = collector.prune_unsuccessful_outputs(output_dir)
            payload = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertEqual(result["invalid_successes"], 1)
            self.assertFalse(render_dir.exists())
            self.assertEqual(payload["succeeded"], 0)
            self.assertEqual(payload["failed"], 1)
            self.assertEqual(payload["videos"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()

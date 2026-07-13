from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import collect_top_video_outputs as collector  # noqa: E402
import scrape_top_videos as scraper  # noqa: E402
from top_video_sources import (  # noqa: E402
    load_processed_source_keys,
    source_key,
    update_processed_sources,
)


def youtube_feed(entries: list[dict[str, str]]) -> bytes:
    rows = []
    for item in entries:
        rows.append(
            f"""
  <entry>
    <yt:videoId>{item['video_id']}</yt:videoId>
    <yt:channelId>{scraper.YOUTUBE_CHANNEL_ID}</yt:channelId>
    <title>{item['title']}</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v={item['video_id']}"/>
    <published>{item['published']}</published>
    <media:group><media:description></media:description></media:group>
  </entry>
"""
        )
    return (
        f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{scraper.ATOM_NS}" xmlns:yt="{scraper.YOUTUBE_NS}" xmlns:media="{scraper.MEDIA_NS}">
  <yt:channelId>{scraper.YOUTUBE_CHANNEL_ID}</yt:channelId>
  <link rel="alternate" href="https://www.youtube.com/channel/{scraper.YOUTUBE_CHANNEL_ID}"/>
  {''.join(rows)}
</feed>
"""
    ).encode()


class TopVideoSourceSelectionTests(unittest.TestCase):
    def test_html_filters_processed_sources_before_max_videos_cutoff(self) -> None:
        urls = [
            "https://www.bloomberg.com/news/videos/2026-07-10/old-one-video",
            "https://www.bloomberg.com/news/videos/2026-07-10/old-two-video",
            "https://www.bloomberg.com/news/videos/2026-07-13/fresh-one-video",
            "https://www.bloomberg.com/news/videos/2026-07-13/fresh-two-video",
        ]
        html = "".join(f'<a href="{url}">{index}</a>' for index, url in enumerate(urls))
        processed = {source_key(urls[0]), source_key(urls[1])}

        selected = scraper.extract_links_from_html(
            html,
            scraper.DEFAULT_URL,
            2,
            skip_leading=0,
            processed_source_keys=processed,
        )

        self.assertEqual([item["url"] for item in selected], urls[2:])

    def test_processed_hero_links_do_not_shift_direct_top_videos_section(self) -> None:
        hero_urls = [
            f"https://www.bloomberg.com/news/videos/2026-07-13/hero-{index}-video"
            for index in range(4)
        ]
        old_top = "https://www.bloomberg.com/news/videos/2026-07-12/old-top-video"
        fresh_top = [
            "https://www.bloomberg.com/news/videos/2026-07-13/fresh-top-one-video",
            "https://www.bloomberg.com/news/videos/2026-07-13/fresh-top-two-video",
        ]
        urls = [*hero_urls, old_top, *fresh_top]
        html = "".join(f'<a href="{url}">{index}</a>' for index, url in enumerate(urls))

        selected = scraper.extract_links_from_html(
            html,
            scraper.DEFAULT_URL,
            2,
            skip_leading=4,
            processed_source_keys={source_key(hero_urls[0]), source_key(old_top)},
        )

        self.assertEqual([item["url"] for item in selected], fresh_top)

    def test_browser_candidates_continue_past_processed_urls_before_cutoff(self) -> None:
        old_urls = [
            "https://www.bloomberg.com/news/videos/2026-07-12/browser-old-one-video",
            "https://www.bloomberg.com/news/videos/2026-07-12/browser-old-two-video",
        ]
        fresh_urls = [
            "https://www.bloomberg.com/news/videos/2026-07-13/browser-fresh-one-video",
            "https://www.bloomberg.com/news/videos/2026-07-13/browser-fresh-two-video",
        ]
        items = [{"url": url, "title": url.rsplit("/", 1)[-1]} for url in [*old_urls, *fresh_urls]]
        processed = {source_key(url) for url in old_urls}

        selected = scraper.normalize_browser_links(
            items,
            2,
            processed_source_keys=processed,
        )
        javascript = scraper.top_videos_js(scraper.TOP_VIDEOS_XPATH, 2, set(old_urls))

        self.assertEqual([item["url"] for item in selected], fresh_urls)
        self.assertLess(javascript.index("excludedUrls.has"), javascript.index("seen.set"))
        self.assertIn("seen.size < maxVideos", javascript)

    def test_youtube_feed_skips_processed_id_and_selects_later_candidate(self) -> None:
        old_id = "PROCESSED01"
        fresh_id = "FRESHVIDEO1"
        videos = scraper.parse_youtube_feed(
            youtube_feed(
                [
                    {
                        "video_id": old_id,
                        "title": "Previously Published Market Interview",
                        "published": "2026-07-13T10:00:00+00:00",
                    },
                    {
                        "video_id": fresh_id,
                        "title": "New Data Center Investment Interview",
                        "published": "2026-07-13T09:00:00+00:00",
                    },
                ]
            ),
            max_videos=1,
            now=datetime(2026, 7, 13, 12, tzinfo=timezone.utc),
            processed_source_keys={source_key("", old_id)},
        )

        self.assertEqual([item["youtube_id"] for item in videos], [fresh_id])

    def test_loads_ledger_and_only_successful_historical_summary_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "processed_sources.json"
            history = root / "history"
            dated = history / "2026-07-12"
            dated.mkdir(parents=True)
            ledger_url = "https://www.bloomberg.com/news/videos/2026-07-10/ledger-video"
            success_youtube_id = "YTSUCCESS01"
            failed_url = "https://www.bloomberg.com/news/videos/2026-07-12/failed-video"
            ledger.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "sources": [
                            {
                                "source_key": source_key(ledger_url),
                                "url": ledger_url,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (dated / "summary.json").write_text(
                json.dumps(
                    {
                        "videos": [
                            {
                                "status": "success",
                                "url": f"https://www.youtube.com/watch?v={success_youtube_id}",
                                "youtube_id": success_youtube_id,
                            },
                            {"status": "failed", "url": failed_url},
                            {"status": "skipped", "url": failed_url + "-skipped"},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            keys = load_processed_source_keys(ledger, history)

        self.assertIn(source_key(ledger_url), keys)
        self.assertIn(source_key("", success_youtube_id), keys)
        self.assertNotIn(source_key(failed_url), keys)


class TopVideoSourceLedgerTests(unittest.TestCase):
    def test_existing_damaged_ledger_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "processed_sources.json"
            ledger.write_text("{not-json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Could not read Top Videos source ledger"):
                load_processed_source_keys(ledger, root / "history")

    def test_records_successes_only_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            summary_path = root / "summary.json"
            ledger_path = root / "processed_sources.json"
            bloomberg_url = "https://www.bloomberg.com/news/videos/2026-07-13/new-video?srnd=homepage"
            youtube_id = "YOUTUBENEW1"
            failed_url = "https://www.bloomberg.com/news/videos/2026-07-13/failed-video"
            summary_path.write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-13",
                        "videos": [
                            {
                                "status": "success",
                                "url": bloomberg_url,
                                "source": "bloomberg",
                                "source_title": "New Bloomberg Video",
                            },
                            {
                                "status": "success",
                                "url": f"https://youtu.be/{youtube_id}?t=10",
                                "source": "youtube-backup",
                                "youtube_id": youtube_id,
                                "title": "New YouTube Video",
                            },
                            {"status": "failed", "url": failed_url},
                            {"status": "skipped", "url": failed_url + "-skipped"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(update_processed_sources(ledger_path, summary_path), 2)
            first = ledger_path.read_bytes()
            self.assertEqual(update_processed_sources(ledger_path, summary_path), 2)
            self.assertEqual(ledger_path.read_bytes(), first)
            payload = json.loads(first)
            records = {item["source_key"]: item for item in payload["sources"]}

        self.assertEqual(set(records), {source_key(bloomberg_url), source_key("", youtube_id)})
        self.assertEqual(
            records[source_key(bloomberg_url)]["url"],
            "https://www.bloomberg.com/news/videos/2026-07-13/new-video",
        )
        self.assertEqual(records[source_key("", youtube_id)]["youtube_id"], youtube_id)
        self.assertEqual(records[source_key(bloomberg_url)]["first_processed_on"], "2026-07-13")
        self.assertEqual(records[source_key(bloomberg_url)]["last_processed_on"], "2026-07-13")

    def test_collector_backfills_all_retained_success_summaries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "top-videos"
            current = history / "2026-07-13"
            previous = history / "2026-07-12"
            current.mkdir(parents=True)
            previous.mkdir()
            ledger = history / "processed_sources.json"
            old_url = "https://www.bloomberg.com/news/videos/2026-07-12/old-success-video"
            new_url = "https://www.bloomberg.com/news/videos/2026-07-13/new-success-video"
            (previous / "summary.json").write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-12",
                        "videos": [{"status": "success", "url": old_url}],
                    }
                ),
                encoding="utf-8",
            )
            (current / "summary.json").write_text(
                json.dumps(
                    {
                        "run_date": "2026-07-13",
                        "videos": [
                            {"status": "success", "url": new_url},
                            {"status": "failed", "url": new_url + "-failed"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            recorded, summary_count = collector.record_successful_source_history(
                ledger,
                history,
                current,
            )
            keys = load_processed_source_keys(ledger, Path(root / "empty-history"))

        self.assertEqual(summary_count, 2)
        self.assertEqual(recorded, 2)
        self.assertEqual(keys, {source_key(old_url), source_key(new_url)})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import resolve_china_show_url as resolver  # noqa: E402


ASSET = "cacc4912-f00c-4162-899e-4c45e5a96234"
OTHER_ASSET = "713a855b-425c-4b47-9f9b-9a228e35fda2"
DAY = "2026-10-26"
LONG_URL = "https://www.bloomberg.com/news/videos/2026-10-26/the-china-show-10-26-2026-video"
SHORT_URL = "https://www.bloomberg.com/news/videos/2026-10-26/the-china-show-10-26-26-video"
NEXT_URL = "https://www.bloomberg.com/news/videos/2026-10-27/the-china-show-12-27-26-video"


def episode_page(url: str, asset: str = ASSET) -> str:
    return (
        f'<meta name="parsely-link" content="{url}"/>'
        '<meta name="parsely-pub-date" content="Oct 26th, 2026"/>'
        f'<meta name="parsely-post-id" content="{asset}"/>'
    )


class ChinaShowResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = contextlib.redirect_stdout(io.StringIO())
        self.output.__enter__()
        self.addCleanup(self.output.__exit__, None, None, None)

    def test_series_uses_published_date_even_when_slug_month_is_wrong(self) -> None:
        # The live October 27 episode is published with a December-looking slug.
        listing = f'<a href="{SHORT_URL}">Previous</a><a href="{NEXT_URL}">Current</a>'
        with patch.object(resolver, "fetch_show_page", return_value=listing) as fetch:
            self.assertEqual(resolver.resolve_china_url("2026-10-27"), NEXT_URL)
        fetch.assert_called_once_with(resolver.CHINA_SERIES_URL, 12)

    def test_series_deduplicates_html_and_escaped_nextjs_links(self) -> None:
        path = SHORT_URL.removeprefix("https://www.bloomberg.com")
        listing = (
            f'<a href="{path}?source=archive&amp;view=all">Show</a>'
            + json.dumps({"url": SHORT_URL}).replace("/", "\\/")
            + json.dumps({"url": SHORT_URL}).replace("/", "\\u002F")
            + f'<a href="{NEXT_URL}">Next</a>'
        )
        self.assertEqual(resolver.china_episode_urls(listing, DAY), [SHORT_URL])

    def test_series_does_not_turn_longer_slug_or_other_show_into_match(self) -> None:
        listing = (
            f'<a href="{SHORT_URL}-trailer">Trailer</a>'
            '<a href="/news/videos/2026-10-26/bloomberg-this-weekend-10-26-26-video">Weekend</a>'
            f'<a href="{NEXT_URL}">Next day</a>'
        )
        self.assertEqual(resolver.china_episode_urls(listing, DAY), [])

    def test_two_distinct_same_day_series_urls_require_explicit_selection(self) -> None:
        listing = f'<a href="{LONG_URL}">One</a><a href="{SHORT_URL}">Two</a>'
        with patch.object(resolver, "fetch_show_page", return_value=listing) as fetch:
            with self.assertRaisesRegex(SystemExit, "Multiple China Show URLs"):
                resolver.resolve_china_url(DAY)
        fetch.assert_called_once()

    def test_missing_series_episode_falls_back_to_verified_full_year_url(self) -> None:
        with patch.object(
            resolver, "fetch_show_page", side_effect=[f'<a href="{NEXT_URL}">Next</a>', episode_page(LONG_URL)]
        ) as fetch:
            self.assertEqual(resolver.resolve_china_url(DAY), LONG_URL)
        self.assertEqual(fetch.call_args_list, [call(resolver.CHINA_SERIES_URL, 12), call(LONG_URL, 12)])

    def test_full_year_404_falls_back_to_verified_short_year_url(self) -> None:
        with patch.object(
            resolver, "fetch_show_page", side_effect=["", RuntimeError("HTTP 404"), episode_page(SHORT_URL)]
        ) as fetch:
            self.assertEqual(resolver.resolve_china_url(DAY), SHORT_URL)
        self.assertEqual(fetch.call_args_list[-2:], [call(LONG_URL, 12), call(SHORT_URL, 12)])

    def test_series_timeout_still_checks_known_episode_urls(self) -> None:
        with patch.object(
            resolver,
            "fetch_show_page",
            side_effect=[subprocess.TimeoutExpired("curl", 12), episode_page(LONG_URL)],
        ):
            self.assertEqual(resolver.resolve_china_url(DAY), LONG_URL)

    def test_http_200_with_only_recommended_assets_is_not_an_episode(self) -> None:
        recommendations = json.dumps({"currentVideo": {"assetId": ASSET}, "recommended": [{"assetId": OTHER_ASSET}]})
        with patch.object(resolver, "fetch_show_page", side_effect=["", recommendations, recommendations]):
            with self.assertRaisesRegex(SystemExit, "No verified China Show URL"):
                resolver.resolve_china_url(DAY)

    def test_stale_http_200_page_bound_to_another_day_is_rejected(self) -> None:
        stale = episode_page(NEXT_URL, OTHER_ASSET)
        with patch.object(resolver, "fetch_show_page", side_effect=["", stale, stale]):
            with self.assertRaisesRegex(SystemExit, "No verified China Show URL"):
                resolver.resolve_china_url(DAY)

    def test_multiple_assets_bound_to_candidate_are_rejected(self) -> None:
        ambiguous = json.dumps({"items": [{"url": LONG_URL, "assetId": ASSET}, {"url": LONG_URL, "assetId": OTHER_ASSET}]})
        with patch.object(resolver, "fetch_show_page", side_effect=["", ambiguous, RuntimeError("HTTP 404")]):
            with self.assertRaisesRegex(SystemExit, "No verified China Show URL"):
                resolver.resolve_china_url(DAY)

    def test_no_published_episode_never_returns_a_neighbouring_date(self) -> None:
        with patch.object(
            resolver,
            "fetch_show_page",
            side_effect=[f'<a href="{NEXT_URL}">Next day</a>', RuntimeError("HTTP 404"), RuntimeError("HTTP 404")],
        ):
            with self.assertRaisesRegex(SystemExit, DAY):
                resolver.resolve_china_url(DAY)

    def run_main(self, arguments: list[str]) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "show.json"
            with patch.object(sys, "argv", ["resolve_china_show_url.py", *arguments, "--metadata", str(metadata)]):
                with patch.dict(os.environ, {"GITHUB_ENV": ""}):
                    resolver.main()
            return json.loads(metadata.read_text())

    def test_explicit_url_bypasses_discovery_and_preserves_metadata(self) -> None:
        with patch.object(resolver, "fetch_show_page", side_effect=AssertionError("must not fetch")):
            result = self.run_main(["--show-date", DAY, "--url", SHORT_URL])
        self.assertEqual(result["SHOW_URL"], SHORT_URL)
        self.assertEqual(result["SHOW_DATE"], DAY)
        self.assertEqual(result["SHOW_TYPE"], "china")
        self.assertEqual(result["SHOW_RESOLUTION_SOURCE"], "explicit")

    def test_explicit_show_date_uses_discovery_and_requested_timeout(self) -> None:
        with patch.object(resolver, "fetch_show_page", return_value=f'<a href="{SHORT_URL}">Show</a>') as fetch:
            result = self.run_main(["--show-date", DAY, "--china-probe-timeout", "7"])
        self.assertEqual(result["SHOW_URL"], SHORT_URL)
        self.assertEqual(result["OUTPUT_DIR"], "rendered-clips/2026-10-26")
        fetch.assert_called_once_with(resolver.CHINA_SERIES_URL, 7)

    def test_weekend_explicit_date_keeps_existing_url_without_china_lookup(self) -> None:
        with patch.object(resolver, "fetch_show_page", side_effect=AssertionError("must not fetch")):
            result = self.run_main(["--show-date", "2026-10-25"])
        self.assertEqual(result["SHOW_TYPE"], "weekend")
        self.assertEqual(result["SHOW_URL"], "https://www.bloomberg.com/news/videos/2026-10-25/bloomberg-this-weekend-10-25-2026-video")
        self.assertEqual(result["SKIP_SHOW"], "false")

    def test_auto_weekend_selection_still_uses_existing_backlog(self) -> None:
        with (
            patch.object(resolver, "default_show_date", return_value="2026-10-25"),
            patch.object(resolver, "load_processed_shows", return_value={"schema_version": 1, "shows": []}),
            patch.object(resolver, "choose_weekend_backlog_item", return_value={"date": "2026-10-18", "source": "weekend-backlog"}) as choose,
            patch.object(resolver, "fetch_show_page", side_effect=AssertionError("must not fetch")),
        ):
            result = self.run_main([])
        self.assertEqual(result["SHOW_DATE"], "2026-10-18")
        self.assertEqual(result["SHOW_RESOLUTION_SOURCE"], "weekend-backlog")
        self.assertEqual(result["SHOW_URL"], "https://www.bloomberg.com/news/videos/2026-10-18/bloomberg-this-weekend-10-18-2026-video")
        choose.assert_called_once()


if __name__ == "__main__":
    unittest.main()

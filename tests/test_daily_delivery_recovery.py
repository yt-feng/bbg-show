from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import resolve_china_show_url as resolver
import scrape_top_videos as scraper


def candidate(index):
    return {"url": f"https://www.bloomberg.com/news/videos/2026-11-02/story-{index}-video",
            "title": f"Story {index}", "slug": f"story_{index}", "source": "bloomberg"}


class DailyDeliveryRecoveryTests(unittest.TestCase):
    def test_show_workflow_only_skips_nonempty_published_show_clips(self):
        workflow = (ROOT / ".github/workflows/daily-china-show.yml").read_text()
        step = workflow.split("- name: Check existing rendered show clips", 1)[1].split("\n      - name:", 1)[0]
        script = textwrap.dedent(step.split("run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            ark = output / "ark-invest"
            ark.mkdir(parents=True)
            (ark / "delivered.mp4").write_bytes(b"ARK video")
            (output / "partial.mp4").touch()
            result_file = Path(tmp) / "result"
            env = {**os.environ, "REPAIR_FAILED_SPEAKERS": "false", "SKIP_SHOW": "false",
                   "OUTPUT_DIR": str(output), "GITHUB_OUTPUT": str(result_file)}
            subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result_file.read_text(), "skip=false\n")
            result_file.unlink()
            (output / "delivered.mp4").write_bytes(b"China Show video")
            subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=True)
            self.assertEqual(result_file.read_text(), "skip=true\n")

    def test_scheduled_backup_preserves_existing_delivery_and_manual_recovery_still_runs(self):
        workflow = (ROOT / ".github/workflows/daily-top-videos.yml").read_text()
        script = textwrap.dedent(re.search(r'python - <<\'PY\' >> "\$GITHUB_OUTPUT"\n(.*?)\n          PY', workflow, re.S).group(1))
        with tempfile.TemporaryDirectory() as tmp:
            today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
            output = Path(tmp) / "rendered-clips/top-videos" / today
            output.mkdir(parents=True)
            for existing, backup, expected in [(False, "true", "true"), (True, "true", "false"), (True, "false", "true")]:
                if existing:
                    (output / "delivered.mp4").touch()
                result = subprocess.run([sys.executable, "-c", script], cwd=tmp, env={**os.environ, "IS_BACKUP": backup}, capture_output=True, text=True, check=True)
                self.assertEqual(result.stdout.strip(), f"needed={expected}")

    def test_final_delivery_check_fails_on_zero_mp4(self):
        workflow = (ROOT / ".github/workflows/daily-top-videos.yml").read_text()
        script = textwrap.dedent(workflow.split("- name: Require a delivered video\n        run: |\n", 1)[1])
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            output.mkdir()
            env = {**os.environ, "OUTPUT_DIR": str(output), "RUN_DATE": "2026-11-02", "GITHUB_STEP_SUMMARY": str(Path(tmp) / "summary")}
            empty = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
            self.assertEqual(empty.returncode, 1)
            (output / "delivered.mp4").touch()
            delivered = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
            self.assertEqual(delivered.returncode, 0)

    def test_rejected_first_nine_are_replaced_from_later_candidates(self):
        items = [candidate(i) for i in range(18)]
        decisions = [{"exclude": True, "reason": "excluded subject"}] * 9 + [{"exclude": False}] * 9
        with patch.object(scraper, "excluded_topic_decision", side_effect=decisions) as classify:
            selected, rejected = scraper.select_eligible_candidates(items, 9, use_ai=True)
        self.assertEqual(selected, items[9:])
        self.assertEqual(len(rejected), 9)
        self.assertEqual(classify.call_count, 18)
        self.assertTrue(classify.call_args.kwargs["use_ai"])

    def test_selection_stops_at_quota_and_deduplicates_candidates(self):
        items = [candidate(1), candidate(1), candidate(2), candidate(3)]
        with patch.object(scraper, "excluded_topic_decision", return_value={"exclude": False}) as classify:
            selected, _ = scraper.select_eligible_candidates(items, 2, use_ai=True)
        self.assertEqual(selected, [candidate(1), candidate(2)])
        self.assertEqual(classify.call_count, 2)

    def test_main_expands_pool_and_keeps_empty_batch_diagnostics_when_backup_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "manifest.json"
            argv = ["scraper", "--semantic-prefilter", "--youtube-backup-videos", "3", "--out", str(out)]
            with (patch.object(sys, "argv", argv),
                  patch.object(scraper, "load_processed_source_keys", return_value=set()),
                  patch.object(scraper, "scrape", return_value=("brp", [candidate(1)])) as scrape,
                  patch.object(scraper, "excluded_topic_decision", return_value={"exclude": True, "reason": "excluded subject"}),
                  patch.object(scraper, "fetch_youtube_feed", side_effect=OSError("HTTP 404"))):
                scraper.main()
            payload = json.loads(out.read_text())
        self.assertEqual(scrape.call_args.args[2], 54)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(payload["selection_status"], "no_eligible_videos")
        self.assertEqual(payload["candidate_count"], 1)
        self.assertEqual(len(payload["prefilter_rejected"]), 1)
        self.assertIn("404", payload["youtube_backup_error"])

    def test_http_fetch_never_starts_a_browser_or_proxy(self):
        with (patch.object(scraper, "fetch_text_direct", side_effect=scraper.FetchError("HTTP 503")),
              patch.object(scraper, "fetch_text_proxy") as proxy,
              patch.object(scraper, "extract_links_with_headless_chrome") as chrome):
            with self.assertRaises(SystemExit):
                scraper.scrape(scraper.DEFAULT_URL, scraper.TOP_VIDEOS_XPATH, 9, "http", 0, 4, Path("unused"))
        proxy.assert_not_called()
        chrome.assert_not_called()

    def run_resolver(self, args):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "show.json"
            with patch.object(sys, "argv", ["resolver", *args, "--metadata", str(out)]), patch.dict(os.environ, {"GITHUB_ENV": ""}):
                resolver.main()
            return json.loads(out.read_text())

    def test_monday_afternoon_selects_monday_china_instead_of_sunday_weekend(self):
        url = "https://www.bloomberg.com/news/videos/2026-11-02/the-china-show-11-02-2026-video"
        with (patch.object(resolver, "default_show_date", return_value="2026-11-01"),
              patch.object(resolver, "current_weekday_show_date", return_value="2026-11-02"),
              patch.object(resolver, "resolve_china_url", return_value=url) as lookup,
              patch.object(resolver, "choose_weekend_backlog_item") as weekend):
            result = self.run_resolver(["--current-weekday"])
        self.assertEqual(result["SHOW_DATE"], "2026-11-02")
        self.assertEqual(result["SHOW_TYPE"], "china")
        self.assertEqual(result["SHOW_RESOLUTION_SOURCE"], "current-weekday")
        lookup.assert_called_once_with("2026-11-02", 12)
        weekend.assert_not_called()

    def test_afternoon_source_not_published_does_not_fall_back_to_yesterday(self):
        with (patch.object(resolver, "current_weekday_show_date", return_value="2026-11-02"),
              patch.object(resolver, "resolve_china_url", side_effect=SystemExit("not published"))):
            with self.assertRaisesRegex(SystemExit, "not published"):
                self.run_resolver(["--current-weekday"])

    def test_current_weekday_does_not_override_manual_settings(self):
        with self.assertRaisesRegex(SystemExit, "explicit show settings"):
            self.run_resolver(["--current-weekday", "--show-date", "2026-10-30"])

    def test_weekday_clock_uses_beijing_and_rejects_saturday(self):
        with patch.object(resolver, "datetime") as clock:
            clock.now.return_value = datetime(2026, 11, 2, 13, 15)
            self.assertEqual(resolver.current_weekday_show_date(), "2026-11-02")
            self.assertEqual(str(clock.now.call_args.args[0]), "Asia/Shanghai")
            clock.now.return_value = datetime(2026, 11, 7, 13, 15)
            with self.assertRaises(SystemExit):
                resolver.current_weekday_show_date()


if __name__ == "__main__":
    unittest.main()

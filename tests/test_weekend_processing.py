from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import cleanup_rendered_clips as cleanup  # noqa: E402
import resolve_china_show_url as resolver  # noqa: E402
from weekend_processed_shows import (  # noqa: E402
    WeekendProcessedShowsError,
    load_processed_shows,
    record_processed_show,
    show_records,
)


class WeekendProcessedShowsTests(unittest.TestCase):
    def test_missing_ledger_is_empty_but_damaged_json_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_shows.json"
            self.assertEqual(load_processed_shows(path), {"schema_version": 1, "shows": []})

            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(WeekendProcessedShowsError, "Cannot read valid JSON"):
                load_processed_shows(path)

            path.write_text('{"schema_version":1,"schema_version":1,"shows":[]}', encoding="utf-8")
            with self.assertRaisesRegex(WeekendProcessedShowsError, "Duplicate JSON object key"):
                load_processed_shows(path)

    def test_schema_rejects_extra_keys_unknown_status_and_duplicate_dates(self) -> None:
        base_record = {
            "show_date": "2026-06-13",
            "status": "rendered",
            "processed_at": "2026-06-13T20:48:58Z",
        }
        invalid_payloads = [
            {"schema_version": 1, "shows": [], "unexpected": True},
            {
                "schema_version": 1,
                "shows": [{**base_record, "status": "pending"}],
            },
            {
                "schema_version": 1,
                "shows": [base_record, dict(base_record)],
            },
            {
                "schema_version": 1,
                "shows": [{**base_record, "processed_at": "2026-06-13T20:48:58+00:00"}],
            },
        ]

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_shows.json"
            for payload in invalid_payloads:
                with self.subTest(payload=payload):
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    with self.assertRaises(WeekendProcessedShowsError):
                        load_processed_shows(path)

    def test_record_is_idempotent_allows_no_eligible_updates_and_never_downgrades_rendered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_shows.json"
            first = record_processed_show(
                path,
                "2026-05-10",
                "no_eligible_speakers",
                processed_at="2026-07-12T01:00:00Z",
            )
            same = record_processed_show(
                path,
                "2026-05-10",
                "no_eligible_speakers",
                processed_at="2026-07-12T02:00:00Z",
            )
            switched = record_processed_show(
                path,
                "2026-05-10",
                "no_eligible_clips",
                processed_at="2026-07-12T03:00:00Z",
            )
            rendered = record_processed_show(
                path,
                "2026-05-10",
                "rendered",
                processed_at="2026-07-12T04:00:00Z",
            )
            downgrade = record_processed_show(
                path,
                "2026-05-10",
                "no_eligible_speakers",
                processed_at="2026-07-12T05:00:00Z",
            )

            self.assertTrue(first.changed)
            self.assertFalse(same.changed)
            self.assertTrue(switched.changed)
            self.assertTrue(rendered.changed)
            self.assertFalse(downgrade.changed)
            record = show_records(load_processed_shows(path))["2026-05-10"]
            self.assertEqual(record["status"], "rendered")
            self.assertEqual(record["processed_at"], "2026-07-12T04:00:00Z")

    def test_seed_uses_successful_publish_commit_times(self) -> None:
        records = show_records(load_processed_shows(ROOT / "rendered-clips/weekend/processed_shows.json"))
        expected_seed_records = {
            "2026-06-13": {
                "show_date": "2026-06-13",
                "status": "rendered",
                "processed_at": "2026-06-13T20:48:58Z",
            },
            "2026-06-14": {
                "show_date": "2026-06-14",
                "status": "rendered",
                "processed_at": "2026-06-15T00:18:26Z",
            },
            "2026-06-28": {
                "show_date": "2026-06-28",
                "status": "rendered",
                "processed_at": "2026-06-28T20:43:25Z",
            },
        }
        for show_date, expected in expected_seed_records.items():
            self.assertEqual(records.get(show_date), expected)

    def test_successful_explicit_rerender_refreshes_retention_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "processed_shows.json"
            record_processed_show(
                path,
                "2026-05-10",
                "rendered",
                processed_at="2026-05-10T12:00:00Z",
            )
            refreshed = record_processed_show(
                path,
                "2026-05-10",
                "rendered",
                processed_at="2026-07-12T12:00:00Z",
                refresh=True,
            )

            record = show_records(load_processed_shows(path))["2026-05-10"]

        self.assertTrue(refreshed.changed)
        self.assertEqual(record["processed_at"], "2026-07-12T12:00:00Z")


class WeekendResolverTests(unittest.TestCase):
    def write_backlog(self, root: Path) -> Path:
        path = root / "backlog.json"
        path.write_text(
            json.dumps(
                {
                    "videos": [
                        {"date": "2026-05-10", "title": "Older Weekend One"},
                        {"date": "2026-05-16", "title": "Older Weekend Two"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        return path

    def choose(
        self,
        backlog: Path,
        rendered_root: Path,
        *,
        processed_dates: set[str] | None = None,
    ) -> dict[str, str] | None:
        return resolver.choose_weekend_backlog_item(
            backlog,
            rendered_root,
            cutoff_date="2026-07-11",
            history_days=30,
            probe_timeout=1,
            probe_availability=True,
            max_history_probes=2,
            processed_dates=processed_dates,
        )

    def test_available_current_weekend_is_preferred_over_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backlog = self.write_backlog(root)
            with patch.object(resolver, "probe_weekend_url", return_value=(True, "available")) as probe:
                selected = self.choose(backlog, root / "rendered")

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["date"], "2026-07-11")
        self.assertEqual(selected["source"], "weekend-current")
        probe.assert_called_once_with("2026-07-11", 1)

    def test_terminal_ledger_dates_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            backlog = self.write_backlog(root)
            with patch.object(resolver, "probe_weekend_url") as probe:
                selected = self.choose(
                    backlog,
                    root / "rendered",
                    processed_dates={"2026-07-11", "2026-05-10"},
                )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["date"], "2026-05-16")
        self.assertEqual(selected["source"], "weekend-backlog")
        probe.assert_not_called()

    def test_existing_mp4_dates_are_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rendered_root = root / "rendered"
            (rendered_root / "2026-05-10").mkdir(parents=True)
            (rendered_root / "2026-05-10" / "clip.mp4").touch()
            backlog = self.write_backlog(root)
            with patch.object(resolver, "probe_weekend_url"):
                selected = self.choose(
                    backlog,
                    rendered_root,
                    processed_dates={"2026-07-11"},
                )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected["date"], "2026-05-16")

    def test_explicit_weekend_date_can_rerun_even_when_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = root / "rendered" / "weekend" / "processed_shows.json"
            record_processed_show(
                ledger,
                "2026-06-13",
                "rendered",
                processed_at="2026-06-13T20:48:58Z",
            )
            metadata = root / "show.json"
            argv = [
                "resolve_china_show_url.py",
                "--show-date",
                "2026-06-13",
                "--show-type",
                "weekend",
                "--rendered-root",
                str(root / "rendered"),
                "--weekend-processed-shows",
                str(ledger),
                "--metadata",
                str(metadata),
            ]
            with patch.object(sys, "argv", argv), patch.object(resolver, "probe_weekend_url") as probe:
                resolver.main()
            payload = json.loads(metadata.read_text(encoding="utf-8"))

        self.assertEqual(payload["SHOW_DATE"], "2026-06-13")
        self.assertEqual(payload["SHOW_RESOLUTION_SOURCE"], "explicit")
        self.assertEqual(payload["SKIP_SHOW"], "false")
        probe.assert_not_called()


class WeekendCleanupTests(unittest.TestCase):
    def test_freshly_rendered_backlog_uses_processed_time_and_keeps_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered_root = Path(tmp) / "rendered-clips"
            fresh_backlog = rendered_root / "2026-05-10"
            old_regular = rendered_root / "2026-05-11"
            fresh_backlog.mkdir(parents=True)
            old_regular.mkdir(parents=True)
            (fresh_backlog / "clip.mp4").touch()
            (old_regular / "clip.mp4").touch()
            ledger = rendered_root / "weekend" / "processed_shows.json"
            record_processed_show(
                ledger,
                "2026-05-10",
                "rendered",
                processed_at="2026-07-12T00:00:00Z",
            )

            argv = [
                "cleanup_rendered_clips.py",
                "--target",
                str(rendered_root),
                "--weekend-processed-shows",
                str(ledger),
                "--retention-hours",
                "72",
                "--now",
                "2026-07-12T12:00:00+08:00",
            ]
            with patch.object(sys, "argv", argv):
                cleanup.main()

            self.assertTrue(fresh_backlog.exists())
            self.assertFalse(old_regular.exists())
            self.assertTrue(ledger.exists())
            self.assertEqual(
                cleanup.expired_date_dirs(
                    rendered_root,
                    cleanup.parse_now("2026-07-15T09:00:01+08:00", ZoneInfo("Asia/Shanghai")),
                    ZoneInfo("Asia/Shanghai"),
                    {"2026-05-10": cleanup.parse_now("2026-07-12T08:00:00+08:00", ZoneInfo("Asia/Shanghai"))},
                ),
                [fresh_backlog],
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import download_bloomberg_video as downloader  # noqa: E402
import show_source_guard as guard  # noqa: E402


ASSET_ONE = "11111111-1111-4111-8111-111111111111"
ASSET_TWO = "22222222-2222-4222-8222-222222222222"
ASSET_THREE = "33333333-3333-4333-8333-333333333333"


def transcript(words: list[str]) -> dict:
    return {
        "duration": 3600.0,
        "segments": [
            {"start": 0, "end": 1800, "text": " ".join(words[: len(words) // 2])},
            {"start": 1800, "end": 3600, "text": " ".join(words[len(words) // 2 :])},
        ],
    }


def identity_for(
    show_date: str,
    asset_id: str,
    words: list[str] | None = None,
    *,
    url: str | None = None,
) -> dict:
    metadata = {"SHOW_DATE": show_date, "SHOW_TYPE": "weekend"}
    plan = {
        "url": url or f"https://www.bloomberg.com/news/videos/{show_date}/show-{show_date}-video",
        "asset_id": asset_id,
    }
    manifest = {"id": asset_id, "showName": "Bloomberg This Weekend", "webTitle": f"Weekend {show_date}"}
    return guard.build_identity(metadata, plan, manifest, transcript(words) if words is not None else None)


class BloombergAssetSelectionTests(unittest.TestCase):
    def test_current_video_asset_wins_over_recommendations(self) -> None:
        probe = {
            "scripts": [
                json.dumps(
                    {
                        "currentVideo": {"assetId": ASSET_TWO, "title": "Requested show"},
                        "recommended": [{"assetId": ASSET_ONE}],
                    }
                )
            ]
        }
        self.assertEqual(
            downloader.choose_asset_id(probe, "https://www.bloomberg.com/news/videos/current", [ASSET_ONE, ASSET_TWO]),
            ASSET_TWO,
        )

    def test_url_bound_asset_wins_over_stale_current_video(self) -> None:
        page_url = "https://www.bloomberg.com/news/videos/2026-07-13/requested-video"
        probe = {
            "scripts": [
                json.dumps(
                    {
                        "currentVideo": {"assetId": ASSET_ONE, "title": "Previous show"},
                        "requested": {"url": page_url, "assetId": ASSET_TWO},
                    }
                )
            ]
        }
        self.assertEqual(
            downloader.choose_asset_id(probe, page_url, [ASSET_ONE, ASSET_TWO]),
            ASSET_TWO,
        )

    def test_ambiguous_unbound_assets_are_rejected_but_one_candidate_is_allowed(self) -> None:
        page_url = "https://www.bloomberg.com/news/videos/2026-07-13/requested-video"
        self.assertIsNone(downloader.choose_asset_id({}, page_url, [ASSET_ONE, ASSET_TWO]))
        self.assertEqual(downloader.choose_asset_id({}, page_url, [ASSET_THREE]), ASSET_THREE)

        with self.assertRaises(SystemExit) as raised:
            downloader.reject_ambiguous_bloomberg_assets(page_url, None, [ASSET_ONE, ASSET_TWO])
        self.assertEqual(raised.exception.code, downloader.SOURCE_NOT_READY_EXIT)


class ShowSourceGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.words = [f"topic{index}" for index in range(500)]

    def test_manifest_date_mismatch_is_stale(self) -> None:
        manifest = {
            "showName": "Bloomberg This Weekend",
            "webTitle": "Bloomberg This Weekend 7/12/2026",
            "sourceURL": "https://www.bloomberg.com/news/videos/2026-07-12/weekend-7-12-2026-video",
        }
        reason = guard.validate_manifest(manifest, "2026-07-13", "weekend")
        self.assertIn("2026-07-12", reason)
        self.assertIn("expected 2026-07-13", reason)
        self.assertEqual(guard.validate_manifest(manifest, "2026-07-12", "weekend"), "")

    def test_asset_url_and_transcript_each_block_cross_date_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "show_sources.json"
            first = identity_for("2026-07-12", ASSET_ONE, self.words)
            args = argparse.Namespace(
                identity=Path(tmp) / "identity.json",
                ledger=ledger_path,
                processed_at="2026-07-12T23:00:00Z",
            )
            args.identity.write_text(json.dumps(first), encoding="utf-8")
            self.assertEqual(guard.record_command(args), 0)
            ledger = guard.load_ledger(ledger_path)

            same_asset = identity_for("2026-07-13", ASSET_ONE)
            matched_date, reason = guard.ledger_duplicate(same_asset, ledger)
            self.assertEqual(matched_date, "2026-07-12")
            self.assertIn("asset", reason)

            same_url = identity_for(
                "2026-07-13",
                ASSET_TWO,
                url="https://www.bloomberg.com/news/videos/2026-07-12/show-2026-07-12-video?utm_source=test",
            )
            matched_date, reason = guard.ledger_duplicate(same_url, ledger)
            self.assertEqual(matched_date, "2026-07-12")
            self.assertIn("URL", reason)

            same_content = identity_for("2026-07-13", ASSET_TWO, self.words)
            matched_date, reason = guard.ledger_duplicate(same_content, ledger)
            self.assertEqual(matched_date, "2026-07-12")
            self.assertIn("transcript", reason)

            almost_same_words = list(self.words)
            for index in range(10):
                almost_same_words[20 + index * 17] = f"correctedword{index}"
            almost_same_content = identity_for("2026-07-13", ASSET_THREE, almost_same_words)
            matched_date, reason = guard.ledger_duplicate(almost_same_content, ledger)
            self.assertEqual(matched_date, "2026-07-12")
            self.assertIn("transcript", reason)

    def test_record_is_idempotent_and_replaces_same_date(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            identity_path = root / "identity.json"
            ledger_path = root / "ledger.json"
            first = identity_for("2026-07-13", ASSET_ONE, self.words)
            identity_path.write_text(json.dumps(first), encoding="utf-8")
            args = argparse.Namespace(
                identity=identity_path,
                ledger=ledger_path,
                processed_at="2026-07-13T23:00:00Z",
            )
            guard.record_command(args)
            guard.record_command(args)
            self.assertEqual(len(guard.load_ledger(ledger_path)["sources"]), 1)

            replacement = identity_for("2026-07-13", ASSET_TWO, self.words + ["updated"])
            identity_path.write_text(json.dumps(replacement), encoding="utf-8")
            guard.record_command(args)
            records = guard.load_ledger(ledger_path)["sources"]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["asset_id"], ASSET_TWO)

    def test_existing_highlight_plan_bootstraps_content_detection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            previous = root / "2026-07-12"
            previous.mkdir()
            clips = []
            for start in (0, 100):
                clip_text = " ".join(self.words[start : start + 100])
                clips.append({"subtitles": [{"en": clip_text}]})
            (previous / "highlight_plan.json").write_text(json.dumps({"clips": clips}), encoding="utf-8")

            current = identity_for("2026-07-13", ASSET_TWO, self.words)
            matched_date, reason = guard.bootstrap_plan_duplicate(current, transcript(self.words), root)

        self.assertEqual(matched_date, "2026-07-12")
        self.assertIn("2 previously published", reason)

    def test_stale_check_is_green_and_sets_duplicate_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / "show.json"
            plan = root / "download_plan.json"
            manifest = root / "manifest.json"
            identity = root / "identity.json"
            outputs = root / "github_output.txt"
            metadata.write_text(
                json.dumps({"SHOW_DATE": "2026-07-13", "SHOW_TYPE": "weekend"}), encoding="utf-8"
            )
            plan.write_text(
                json.dumps({"url": "https://www.bloomberg.com/news/videos/2026-07-13/weekend", "asset_id": ASSET_ONE}),
                encoding="utf-8",
            )
            manifest.write_text(
                json.dumps({"id": ASSET_ONE, "webTitle": "Bloomberg This Weekend 7/12/2026"}),
                encoding="utf-8",
            )
            args = argparse.Namespace(
                show_metadata=metadata,
                download_plan=plan,
                manifest=manifest,
                transcript=None,
                ledger=root / "missing-ledger.json",
                rendered_root=root / "rendered-clips",
                identity_output=identity,
                github_output=outputs,
            )
            self.assertEqual(guard.check_command(args), 0)
            output_text = outputs.read_text(encoding="utf-8")

        self.assertIn("duplicate=true", output_text)
        self.assertIn("expected 2026-07-13", output_text)

    def test_invalid_optional_manifest_does_not_hide_asset_and_transcript_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "embed_manifest.json"
            path.write_text("not-json", encoding="utf-8")
            self.assertEqual(guard.optional_manifest(path), {})

    def test_workflow_keeps_download_metadata_and_gates_publish_steps(self) -> None:
        workflow = (ROOT / ".github/workflows/daily-china-show.yml").read_text(encoding="utf-8")
        self.assertIn("--keep-tmp", workflow)
        self.assertIn("id: download", workflow)
        self.assertIn('if [ "$status" -eq 75 ]', workflow)
        self.assertIn('echo "source_ready=false" >> "$GITHUB_OUTPUT"', workflow)
        self.assertIn("steps.download.outputs.source_ready == 'true'", workflow)
        self.assertIn("id: source_guard_initial", workflow)
        self.assertIn("id: source_guard_transcript", workflow)
        self.assertIn("steps.source_guard_initial.outputs.duplicate != 'true'", workflow)
        self.assertIn("steps.source_guard_transcript.outputs.duplicate != 'true'", workflow)
        self.assertIn("--ledger \"$SHOW_SOURCE_LEDGER\"", workflow)


if __name__ == "__main__":
    unittest.main()

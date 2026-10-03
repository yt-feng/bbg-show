from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import process_ark_videos as process
from verify_ark_outputs import verify


class ArkOutcomeTests(unittest.TestCase):
    def test_real_manifest_filter_records_all_exclusions_without_processing_or_state_adoption(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"videos": [{"url": "https://example.test/itk", "title": "ITK"}]}))
            argv = ["process", "--manifest", str(manifest), "--run-date", "2026-10-03",
                    "--out-root", str(root / "out"), "--work-root", str(root / "work"),
                    "--state", str(root / "state.json"), "--wistia-source-ledger", str(root / "wistia.json")]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(process, "is_trump_related", return_value=True), \
                    mock.patch.object(process, "process_one") as render, \
                    mock.patch.object(process, "archive_successful_transcripts"):
                process.main()
            render.assert_not_called()
            summary = json.loads((root / "out/2026-10-03/ark-invest/summary.json").read_text())
            self.assertEqual(verify(json.loads(manifest.read_text()), summary)["outcome"], "all_selected_sources_excluded")
            self.assertFalse((root / "state.json").exists())

    def fixture(self, root):
        manifest = {"videos": [{"url": "a"}]}
        summary = {"output_dir": str(root), "total": 1, "succeeded": 0, "failed": 0, "skipped": 1,
                   "videos": [{"url": "a", "status": "skipped", "reason": "sensitive_topic"}]}
        return manifest, summary

    def test_rejects_empty_or_unexplained_decisions(self):
        manifest, summary = self.fixture(Path("."))
        for changes in ({"videos": []}, {"videos": [{"url": "a", "status": "skipped", "reason": "download_error"}]},
                        {"videos": [{"url": "a", "status": "failed"}]}, {"skipped": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify(manifest, summary | changes)

    def test_rejects_success_without_real_clip_and_accepts_nonempty_scoped_clip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, summary = self.fixture(root)
            summary.update(succeeded=1, skipped=0, videos=[{"url": "a", "status": "success",
                           "output_dir": str(root), "rendered_files": ["clip.mp4"]}])
            with self.assertRaises(ValueError):
                verify(manifest, summary)
            (root / "clip.mp4").write_bytes(b"test render fixture")
            self.assertEqual(verify(manifest, summary)["rendered_clips"], 1)
            summary["videos"][0]["rendered_files"] = ["../escape.mp4"]
            with self.assertRaises(ValueError):
                verify(manifest, summary)

    def test_runtime_sensitive_skip_is_recognized_but_download_error_is_not(self):
        self.assertTrue(process.is_sensitive_skip_output("ARK video skipped by sensitive topic filter"))
        self.assertFalse(process.is_sensitive_skip_output("Download failed: no video"))


if __name__ == "__main__":
    unittest.main()

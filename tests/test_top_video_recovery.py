from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_top_video_recovery", ROOT / "tools" / "validate_top_video_recovery.py"
)
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class TopVideoRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = "yt-feng/bbg-show"
        self.run_id = "33697363587"
        self.run_date = "2026-09-03"
        self.run = {
            "id": int(self.run_id),
            "repository": {"full_name": self.repository},
            "head_repository": {"full_name": self.repository},
            "path": recovery.WORKFLOW_PATH,
            "head_branch": "main",
            "event": "schedule",
            "status": "completed",
            "conclusion": "failure",
        }
        self.artifacts = [
            {"name": "top-videos-manifest", "expired": False},
            {"name": "top-video-2026-09-03-2", "expired": False},
            {"name": "top-video-2026-09-03-1", "expired": False},
        ]

    def validate(self, run=None, artifacts=None, run_date=None):
        return recovery.validate_source(
            self.run if run is None else run,
            self.artifacts if artifacts is None else artifacts,
            self.repository,
            self.run_id,
            self.run_date if run_date is None else run_date,
        )

    def test_completed_failed_run_can_recover_partial_video_artifacts(self) -> None:
        self.assertEqual(self.validate(), [1, 2])

    def test_rejects_wrong_repository_workflow_branch_and_incomplete_source(self) -> None:
        for key, value in (
            ("repository", {"full_name": "someone/other"}),
            ("head_repository", {"full_name": "someone/bbg-show"}),
            ("path", ".github/workflows/another.yml"),
            ("head_branch", "feature"),
            ("event", "pull_request"),
            ("status", "in_progress"),
            ("id", 123),
        ):
            with self.subTest(field=key):
                run = deepcopy(self.run)
                run[key] = value
                with self.assertRaises(ValueError):
                    self.validate(run=run)

    def test_rejects_date_mismatch_even_if_one_artifact_matches(self) -> None:
        with self.assertRaisesRegex(ValueError, "dates"):
            self.validate(run_date="2026-09-02")
        artifacts = deepcopy(self.artifacts)
        artifacts.append({"name": "top-video-2026-09-02-3", "expired": False})
        with self.assertRaisesRegex(ValueError, "dates"):
            self.validate(artifacts=artifacts)

    def test_rejects_expired_or_missing_manifest_and_outputs(self) -> None:
        for artifacts in (
            self.artifacts[1:],
            self.artifacts[:1],
            [*self.artifacts, self.artifacts[0]],
        ):
            with self.subTest(artifacts=artifacts), self.assertRaises(ValueError):
                self.validate(artifacts=artifacts)
        for index in range(len(self.artifacts)):
            with self.subTest(expired_index=index):
                artifacts = deepcopy(self.artifacts)
                artifacts[index]["expired"] = True
                with self.assertRaises(ValueError):
                    self.validate(artifacts=artifacts)

    def test_rejects_ambiguous_or_invalid_video_artifact_indexes(self) -> None:
        for name in ("top-video-2026-09-03-2", "top-video-2026-09-03-13", "top-video-2026-09-03-0"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.validate(artifacts=[*self.artifacts, {"name": name, "expired": False}])

    def test_inputs_are_strict_before_any_network_lookup(self) -> None:
        for run_id in ("", "0", "12/other", "123\n", "１２３", "${{ github.run_id }}"):
            with self.subTest(run_id=run_id), self.assertRaises(ValueError):
                recovery.validate_inputs(run_id, self.run_date)
        for run_date in ("2026-9-03", "20260903", "2026-02-30", "2026-09-03\n"):
            with self.subTest(run_date=run_date), self.assertRaises(ValueError):
                recovery.validate_inputs(self.run_id, run_date)


if __name__ == "__main__":
    unittest.main()

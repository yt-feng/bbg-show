from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import cleanup_rendered_clips as cleanup  # noqa: E402


class CleanupRenderedClipsTests(unittest.TestCase):
    def run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )

    def write_file(self, path: Path, text: str = "clip") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def seed_repo(self, root: Path) -> None:
        self.run_git(root, "init")
        self.run_git(root, "config", "user.name", "test")
        self.run_git(root, "config", "user.email", "test@example.com")

        self.write_file(root / "rendered-clips/2026-08-04/clip.mp4")
        self.write_file(root / "rendered-clips/2026-08-06/clip.mp4")
        self.write_file(root / "rendered-clips/top-videos/2026-08-04/item/clip.mp4")
        self.write_file(root / "rendered-clips/top-videos/2026-08-06/item/clip.mp4")
        self.write_file(
            root / "rendered-clips/weekend/processed_shows.json",
            json.dumps({"schema_version": 1, "shows": []}),
        )

        self.run_git(root, "add", ".")
        self.run_git(root, "commit", "-m", "seed rendered clips")

    def test_git_index_mode_removes_expired_dirs_without_worktree_blobs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.seed_repo(root)
            self.run_git(root, "sparse-checkout", "init", "--no-cone")
            self.run_git(root, "sparse-checkout", "set", "--no-cone", "rendered-clips/weekend/processed_shows.json")

            self.assertFalse((root / "rendered-clips/2026-08-04/clip.mp4").exists())
            previous_cwd = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(
                    sys,
                    "argv",
                    [
                        "cleanup_rendered_clips.py",
                        "--retention-hours",
                        "72",
                        "--now",
                        "2026-08-08T00:00:00+08:00",
                        "--git-index",
                    ],
                ):
                    cleanup.main()
            finally:
                os.chdir(previous_cwd)

            diff = self.run_git(root, "diff", "--cached", "--name-status", "--", "rendered-clips").stdout

        self.assertIn("D\trendered-clips/2026-08-04/clip.mp4", diff)
        self.assertIn("D\trendered-clips/top-videos/2026-08-04/item/clip.mp4", diff)
        self.assertNotIn("2026-08-06", diff)


if __name__ == "__main__":
    unittest.main()

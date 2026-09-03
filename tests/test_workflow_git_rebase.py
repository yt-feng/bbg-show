"""Offline regression for concurrent rendered-clip publication and cleanup.

Uses only temporary local repositories. No network, GitHub account, or media
encoder is needed: the byte fixtures model similar binary MP4 outputs.
"""

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class WorkflowGitRebaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="bbg-show-git-rebase-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        # Keep developer/CI Git environment overrides out of the fixture.
        self.env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Workflow regression",
            GIT_AUTHOR_EMAIL="workflow@example.invalid",
            GIT_COMMITTER_NAME="Workflow regression",
            GIT_COMMITTER_EMAIL="workflow@example.invalid",
            GIT_EDITOR="true",
            GIT_SEQUENCE_EDITOR="true",
        )
        self.origin = self.root / "origin.git"
        self.publisher = self.root / "publisher"
        self.cleanup = self.root / "cleanup"
        self.git(self.root, "init", "--bare", "--initial-branch=main", str(self.origin))
        self.git(self.root, "clone", str(self.origin), str(self.publisher))
        self.write(self.publisher, "README.md", b"original documentation\n")
        self.write(self.publisher, "obsolete.txt", b"expired remote-only file\n")
        self.old_path = "rendered-clips/old-clip.mp4"
        self.new_path = "rendered-clips/new-clip.mp4"
        # Both fixtures are binary. Keeping most blocks identical mirrors MP4s
        # generated from the same visual template and audio/background assets.
        common = b"\x00" + b"same-template-video-frame\n" * 600
        self.old_bytes = common + b"old-rendered-audio-block\n" * 20
        self.new_bytes = common + b"new-rendered-audio-block\n" * 20
        self.write(self.publisher, self.old_path, self.old_bytes)
        self.commit_all(self.publisher, "Initial files and old rendered clip")
        self.git(self.publisher, "push", "origin", "main")
        self.git(self.root, "clone", str(self.origin), str(self.cleanup))

        # The collector works from the old remote tip while generating a new
        # publication commit. The old clip expires during the same run.
        (self.publisher / self.old_path).unlink()
        self.write(self.publisher, self.new_path, self.new_bytes)
        self.write(self.publisher, "generated.json", b'{"clip":"new-clip.mp4"}\n')
        self.commit_all(self.publisher, "Publish generated clip")
        self.publish_patch = self.git(
            self.publisher, "show", "--format=", "--name-status", "--find-renames", "HEAD"
        ).stdout

        # A concurrent cleanup job already removes that old clip. It also
        # modifies/deletes unrelated paths that rebasing must preserve.
        (self.cleanup / self.old_path).unlink()
        (self.cleanup / "obsolete.txt").unlink()
        self.write(self.cleanup, "README.md", b"concurrent remote documentation\n")
        self.commit_all(self.cleanup, "Concurrent cleanup and documentation update")
        self.git(self.cleanup, "push", "origin", "main")
        self.remote_tip = self.git(self.cleanup, "rev-parse", "HEAD").stdout.strip()

    def git(self, cwd, *args, check=True):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull,
             "-c", "commit.gpgsign=false", *args],
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
        )
        if check and result.returncode:
            self.fail(
                "git " + " ".join(args) + " failed:\n" + result.stdout + result.stderr
            )
        return result

    @staticmethod
    def write(repo, path, data):
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def commit_all(self, repo, message):
        self.git(repo, "add", "-A")
        self.git(repo, "commit", "-m", message)

    def assert_successful_publication(self):
        self.assertEqual((self.publisher / self.new_path).read_bytes(), self.new_bytes)
        self.assertFalse((self.publisher / self.old_path).exists())
        self.assertFalse((self.publisher / "obsolete.txt").exists())
        self.assertEqual(
            (self.publisher / "README.md").read_bytes(),
            b"concurrent remote documentation\n",
        )
        self.assertEqual(
            (self.publisher / "generated.json").read_bytes(),
            b'{"clip":"new-clip.mp4"}\n',
        )
        self.assertEqual(self.git(self.publisher, "status", "--porcelain").stdout, "")
        self.git(self.publisher, "merge-base", "--is-ancestor", self.remote_tip, "HEAD")
        # A non-force push must work after the corrected rebase.
        self.git(self.publisher, "push", "origin", "main")
        self.assertEqual(
            self.git(self.origin, "rev-parse", "main").stdout,
            self.git(self.publisher, "rev-parse", "HEAD").stdout,
        )

    def test_default_rebase_reproduces_binary_rename_delete_conflict(self):
        self.assertRegex(self.publish_patch, r"R\d+\s+rendered-clips/old-clip.mp4")
        result = self.git(self.publisher, "pull", "--rebase", "origin", "main", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFLICT (rename/delete)", result.stdout + result.stderr)
        self.assertIn("CONFLICT (modify/delete)", result.stdout + result.stderr)
        self.assertNotEqual(self.git(self.publisher, "ls-files", "--unmerged").stdout, "")

    def test_merge_renames_false_preserves_publication_and_concurrent_changes(self):
        self.git(self.publisher, "-c", "merge.renames=false", "pull", "--rebase", "origin", "main")
        self.assert_successful_publication()

    def test_disabled_rename_detection_still_reports_real_content_conflicts(self):
        self.write(self.publisher, "README.md", b"conflicting local documentation\n")
        self.commit_all(self.publisher, "Conflicting local documentation update")
        result = self.git(
            self.publisher, "-c", "merge.renames=false", "pull", "--rebase", "origin", "main",
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CONFLICT (content): Merge conflict in README.md", result.stdout + result.stderr)
        self.assertIn("README.md", self.git(self.publisher, "ls-files", "--unmerged").stdout)
        self.assertEqual(self.git(self.origin, "rev-parse", "main").stdout.strip(), self.remote_tip)


if __name__ == "__main__":
    unittest.main(verbosity=2)

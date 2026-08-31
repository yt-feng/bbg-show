from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import verify_encrypted_transcript_tree as verifier  # noqa: E402


ARCHIVE_BASENAME = f"{'a' * 64}__{'b' * 64}"


class EncryptedTranscriptTreeTests(unittest.TestCase):
    def test_accepts_documentation_and_encrypted_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "transcripts"
            archive_dir = root / "top-videos" / "2026-08-31"
            archive_dir.mkdir(parents=True)
            (root / "README.md").write_text("archive documentation\n", encoding="utf-8")
            (archive_dir / f"{ARCHIVE_BASENAME}.json.cms").write_bytes(b"encrypted-json")
            (archive_dir / f"{ARCHIVE_BASENAME}.md.cms").write_bytes(b"encrypted-markdown")

            self.assertEqual(verifier.verify_tree(root), 2)

    def test_rejects_plaintext_and_unexpected_layouts(self) -> None:
        invalid_paths = (
            Path("shows/2026-08-31/transcript.json"),
            Path("shows/2026/08/31/archive.json.cms"),
            Path("other/2026-08-31") / f"{ARCHIVE_BASENAME}.json.cms",
            Path("shows/not-a-date") / f"{ARCHIVE_BASENAME}.json.cms",
            Path("shows/2026-08-31/readable.md"),
        )
        for invalid_path in invalid_paths:
            with self.subTest(path=invalid_path), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "transcripts"
                target = root / invalid_path
                target.parent.mkdir(parents=True)
                target.write_bytes(b"not allowed")
                with self.assertRaises(verifier.ArchiveTreeError):
                    verifier.verify_tree(root)

    def test_rejects_empty_ciphertext_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "transcripts"
            archive_dir = root / "shows" / "2026-08-31"
            archive_dir.mkdir(parents=True)
            empty = archive_dir / f"{ARCHIVE_BASENAME}.json.cms"
            empty.touch()
            with self.assertRaises(verifier.ArchiveTreeError):
                verifier.verify_tree(root)

            empty.write_bytes(b"ciphertext")
            link = archive_dir / f"{'c' * 64}__{'d' * 64}.md.cms"
            link.symlink_to(empty)
            with self.assertRaises(verifier.ArchiveTreeError):
                verifier.verify_tree(root)

    def test_rejects_incomplete_encrypted_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "transcripts"
            archive_dir = root / "ark-invest" / "2026-08-31"
            archive_dir.mkdir(parents=True)
            (archive_dir / f"{ARCHIVE_BASENAME}.json.cms").write_bytes(b"encrypted-json")

            with self.assertRaises(verifier.ArchiveTreeError):
                verifier.verify_tree(root)


if __name__ == "__main__":
    unittest.main()

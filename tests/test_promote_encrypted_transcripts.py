from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import promote_encrypted_transcripts as promote  # noqa: E402


class PromoteEncryptedTranscriptsTests(unittest.TestCase):
    def make_pair(
        self,
        staging_root: Path,
        *,
        container_name: str = "01_video",
        source_hash: str = "a" * 64,
        transcript_hash: str = "b" * 64,
        json_payload: bytes = b"json-ciphertext",
        markdown_payload: bytes = b"markdown-ciphertext",
    ) -> tuple[Path, Path, Path]:
        archive_dir = staging_root / container_name / promote.ARCHIVE_DIRECTORY_NAME
        archive_dir.mkdir(parents=True)
        basename = f"{source_hash}__{transcript_hash}"
        json_cms = archive_dir / f"{basename}.json.cms"
        markdown_cms = archive_dir / f"{basename}.md.cms"
        json_cms.write_bytes(json_payload)
        markdown_cms.write_bytes(markdown_payload)
        return archive_dir, json_cms, markdown_cms

    def test_promotes_one_pair_and_removes_only_empty_staging_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_root = root / "staging"
            output_dir = root / "transcripts"
            archive_dir, json_cms, markdown_cms = self.make_pair(staging_root)
            retained_clip = archive_dir.parent / "rendered.mp4"
            retained_clip.write_bytes(b"video")

            result = promote.promote_encrypted_transcripts(staging_root, output_dir)

            self.assertEqual(result.staging_directories, 1)
            self.assertEqual(result.promoted_count, 2)
            self.assertEqual(result.existing_count, 0)
            self.assertEqual((output_dir / json_cms.name).read_bytes(), b"json-ciphertext")
            self.assertEqual(
                (output_dir / markdown_cms.name).read_bytes(),
                b"markdown-ciphertext",
            )
            self.assertFalse(archive_dir.exists())
            self.assertTrue(retained_clip.is_file())

    def test_promotes_multiple_pairs_from_direct_child_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_root = root / "staging"
            output_dir = root / "transcripts"
            first_archive, first_json, first_markdown = self.make_pair(
                staging_root,
                container_name="01_first",
                source_hash="1" * 64,
                transcript_hash="2" * 64,
            )
            second_archive, second_json, second_markdown = self.make_pair(
                staging_root,
                container_name="02_second",
                source_hash="3" * 64,
                transcript_hash="4" * 64,
            )

            result = promote.promote_encrypted_transcripts(staging_root, output_dir)

            expected_names = sorted(
                path.name for path in (first_json, first_markdown, second_json, second_markdown)
            )
            self.assertEqual(result.staging_directories, 2)
            self.assertEqual(list(result.promoted_files), expected_names)
            self.assertEqual(sorted(path.name for path in output_dir.iterdir()), expected_names)
            self.assertFalse(first_archive.parent.exists())
            self.assertFalse(second_archive.parent.exists())

    def test_existing_content_addressed_pair_is_preserved_and_rerun_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_root = root / "staging"
            output_dir = root / "transcripts"
            archive_dir, json_cms, markdown_cms = self.make_pair(
                staging_root,
                json_payload=b"new-randomized-json-ciphertext",
                markdown_payload=b"new-randomized-markdown-ciphertext",
            )
            output_dir.mkdir()
            (output_dir / json_cms.name).write_bytes(b"preserved-json-ciphertext")
            (output_dir / markdown_cms.name).write_bytes(b"preserved-markdown-ciphertext")

            first = promote.promote_encrypted_transcripts(staging_root, output_dir)
            second = promote.promote_encrypted_transcripts(staging_root, output_dir)

            self.assertEqual(first.promoted_count, 0)
            self.assertEqual(first.existing_count, 2)
            self.assertEqual(first.staging_directories, 1)
            self.assertEqual(
                (output_dir / json_cms.name).read_bytes(),
                b"preserved-json-ciphertext",
            )
            self.assertEqual(
                (output_dir / markdown_cms.name).read_bytes(),
                b"preserved-markdown-ciphertext",
            )
            self.assertFalse(archive_dir.parent.exists())
            self.assertEqual(second, promote.PromotionResult((), (), 0))

    def test_missing_staging_root_and_root_without_archives_are_noops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output_dir = root / "transcripts"

            missing = promote.promote_encrypted_transcripts(root / "missing", output_dir)
            (root / "staging" / "ordinary-output").mkdir(parents=True)
            empty = promote.promote_encrypted_transcripts(root / "staging", output_dir)

            self.assertEqual(missing, promote.PromotionResult((), (), 0))
            self.assertEqual(empty, promote.PromotionResult((), (), 0))
            self.assertFalse(output_dir.exists())

    def test_invalid_staging_is_rejected_before_any_files_are_promoted(self) -> None:
        cases = (
            "extra file",
            "missing pair member",
            "mismatched basenames",
            "empty file",
            "nested directory",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                staging_root = root / "staging"
                output_dir = root / "transcripts"
                first_archive, first_json, first_markdown = self.make_pair(
                    staging_root,
                    container_name="01_valid",
                    source_hash="1" * 64,
                    transcript_hash="2" * 64,
                )
                invalid_archive, invalid_json, invalid_markdown = self.make_pair(
                    staging_root,
                    container_name="02_invalid",
                    source_hash="3" * 64,
                    transcript_hash="4" * 64,
                )

                if case == "extra file":
                    (invalid_archive / "extra.cms").write_bytes(b"extra")
                elif case == "missing pair member":
                    invalid_markdown.unlink()
                elif case == "mismatched basenames":
                    invalid_markdown.rename(
                        invalid_archive / f"{'5' * 64}__{'6' * 64}.md.cms"
                    )
                elif case == "empty file":
                    invalid_json.write_bytes(b"")
                elif case == "nested directory":
                    invalid_markdown.unlink()
                    (invalid_archive / "nested").mkdir()

                with self.assertRaises(promote.PromotionError):
                    promote.promote_encrypted_transcripts(staging_root, output_dir)

                self.assertFalse(output_dir.exists())
                self.assertTrue(first_json.is_file())
                self.assertTrue(first_markdown.is_file())
                self.assertTrue(first_archive.is_dir())

    def test_symlinks_and_overlapping_output_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_root = root / "staging"
            archive_dir, json_cms, _markdown_cms = self.make_pair(staging_root)
            target = root / "ciphertext-target"
            target.write_bytes(b"ciphertext")
            json_cms.unlink()
            os.symlink(target, json_cms)

            with self.assertRaises(promote.PromotionError):
                promote.promote_encrypted_transcripts(staging_root, root / "transcripts")

            self.assertFalse((root / "transcripts").exists())
            self.assertTrue(archive_dir.is_dir())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging_root = root / "staging"
            _archive_dir, json_cms, _markdown_cms = self.make_pair(staging_root)

            with self.assertRaises(promote.PromotionError):
                promote.promote_encrypted_transcripts(
                    staging_root,
                    staging_root / "published",
                )

            self.assertTrue(json_cms.is_file())


if __name__ == "__main__":
    unittest.main()

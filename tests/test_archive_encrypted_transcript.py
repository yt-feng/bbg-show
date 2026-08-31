from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import archive_encrypted_transcript as archive  # noqa: E402


OPENSSL = shutil.which("openssl")


@unittest.skipUnless(OPENSSL, "OpenSSL is required for CMS archive tests")
class EncryptedTranscriptArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.key_material = tempfile.TemporaryDirectory(prefix="archive-test-key-")
        key_root = Path(cls.key_material.name)
        cls.private_key = key_root / "private.pem"
        cls.wrong_private_key = key_root / "wrong-private.pem"
        cls.certificate = key_root / "recipient.pem"
        subprocess.run(
            [
                OPENSSL,
                "req",
                "-x509",
                "-newkey",
                "rsa:3072",
                "-keyout",
                str(cls.private_key),
                "-out",
                str(cls.certificate),
                "-nodes",
                "-subj",
                "/CN=bbg-show-transcript-test",
                "-days",
                "1",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        subprocess.run(
            [
                OPENSSL,
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:3072",
                "-out",
                str(cls.wrong_private_key),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.key_material.cleanup()

    def write_inputs(self, root: Path) -> tuple[Path, Path, dict[str, object], dict[str, object]]:
        marker = "PRIVATE-TRANSCRIPT-MARKER-67f81ee15ed742ff"
        source: dict[str, object] = {
            "kind": "show",
            "published_date": "2026-08-31",
            "source_url": "https://example.invalid/video/secret-source",
            "title": "Confidential market discussion",
        }
        transcript: dict[str, object] = {
            "duration": 65.75,
            "language": "en",
            "model": "base",
            "segments": [
                {"start": 0, "end": 2.5, "text": f"Opening {marker}."},
                {"start": 60.25, "end": 65.75, "text": "第二段 transcript text."},
            ],
        }
        transcript_path = root / "input" / "transcript.json"
        source_path = root / "input" / "source.json"
        transcript_path.parent.mkdir(parents=True)
        transcript_path.write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        source_path.write_text(
            json.dumps(source, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return transcript_path, source_path, transcript, source

    def decrypt_attempt(
        self,
        cms_path: Path,
        *,
        private_key: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [
                OPENSSL,
                "cms",
                "-decrypt",
                "-binary",
                "-inform",
                "DER",
                "-in",
                str(cms_path),
                "-recip",
                str(self.certificate),
                "-inkey",
                str(private_key or self.private_key),
            ],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def decrypt(self, cms_path: Path) -> bytes:
        result = self.decrypt_attempt(cms_path)
        result.check_returncode()
        return result.stdout

    def decrypt_to_authenticated_output(
        self,
        cms_path: Path,
        final_output: Path,
        *,
        private_key: Path | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        """Model the local decrypt script's fail-closed publication boundary."""

        final_output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="authenticated-decrypt-",
            dir=final_output.parent,
        ) as raw_temp_dir:
            private_temp_dir = Path(raw_temp_dir)
            self.assertEqual(stat.S_IMODE(private_temp_dir.stat().st_mode), 0o700)
            temporary_output = private_temp_dir / "plaintext.tmp"
            result = subprocess.run(
                [
                    OPENSSL,
                    "cms",
                    "-decrypt",
                    "-binary",
                    "-inform",
                    "DER",
                    "-in",
                    str(cms_path),
                    "-recip",
                    str(self.certificate),
                    "-inkey",
                    str(private_key or self.private_key),
                    "-out",
                    str(temporary_output),
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode == 0:
                os.replace(temporary_output, final_output)
            else:
                temporary_output.unlink(missing_ok=True)
            return result

    def cms_structure(self, cms_path: Path) -> str:
        result = subprocess.run(
            [
                OPENSSL,
                "cms",
                "-cmsout",
                "-inform",
                "DER",
                "-in",
                str(cms_path),
                "-print",
                "-noout",
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        return result.stdout

    def test_round_trip_uses_required_cms_algorithms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, transcript, source = self.write_inputs(root)
            result = archive.archive_transcript(
                transcript_path,
                source_path,
                self.certificate,
                root / "archives",
                openssl_binary=OPENSSL,
            )

            decrypted_json = json.loads(self.decrypt(result.json_cms))
            decrypted_markdown = self.decrypt(result.markdown_cms).decode("utf-8")
            structures = [self.cms_structure(result.json_cms), self.cms_structure(result.markdown_cms)]

        self.assertTrue(result.created)
        self.assertEqual(decrypted_json["archive_format"], archive.ARCHIVE_FORMAT)
        self.assertEqual(decrypted_json["schema_version"], 1)
        self.assertEqual(decrypted_json["source"], source)
        self.assertEqual(decrypted_json["transcript"], transcript)
        self.assertEqual(decrypted_json["source_sha256"], archive.json_sha256(source))
        self.assertEqual(decrypted_json["transcript_sha256"], archive.json_sha256(transcript))
        self.assertIn("PRIVATE-TRANSCRIPT-MARKER-67f81ee15ed742ff", decrypted_markdown)
        self.assertIn("[00:01:00.250 - 00:01:05.750]", decrypted_markdown)
        for structure in structures:
            self.assertIn("id-smime-ct-authEnvelopedData", structure)
            self.assertIn("rsaesOaep", structure)
            self.assertIn("aes-256-gcm", structure)
            self.assertGreaterEqual(structure.count(":sha256"), 2)

    def test_content_addressed_rerun_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, transcript, source = self.write_inputs(root)
            output_dir = root / "archives"
            first = archive.archive_transcript(
                transcript_path,
                source_path,
                self.certificate,
                output_dir,
                openssl_binary=OPENSSL,
            )
            original_payloads = {
                first.json_cms.name: first.json_cms.read_bytes(),
                first.markdown_cms.name: first.markdown_cms.read_bytes(),
            }
            second = archive.archive_transcript(
                transcript_path,
                source_path,
                self.certificate,
                output_dir,
                openssl_binary=OPENSSL,
            )

            names = sorted(path.name for path in output_dir.iterdir())
            current_payloads = {path.name: path.read_bytes() for path in output_dir.iterdir()}

        expected_prefix = f"{archive.json_sha256(source)}__{archive.json_sha256(transcript)}"
        self.assertFalse(second.created)
        self.assertEqual(first.json_cms, second.json_cms)
        self.assertEqual(first.markdown_cms, second.markdown_cms)
        self.assertEqual(
            names,
            [f"{expected_prefix}.json.cms", f"{expected_prefix}.md.cms"],
        )
        self.assertEqual(current_payloads, original_payloads)

    def test_output_directory_contains_only_ciphertext_cms_files(self) -> None:
        marker = b"PRIVATE-TRANSCRIPT-MARKER-67f81ee15ed742ff"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, _transcript, _source = self.write_inputs(root)
            output_dir = root / "archives"
            archive.archive_transcript(
                transcript_path,
                source_path,
                self.certificate,
                output_dir,
                openssl_binary=OPENSSL,
            )
            outputs = list(output_dir.iterdir())

            self.assertEqual(len(outputs), 2)
            for output in outputs:
                self.assertTrue(output.is_file())
                self.assertEqual(output.suffix, ".cms")
                self.assertRegex(
                    output.name,
                    re.compile(r"^[0-9a-f]{64}__[0-9a-f]{64}\.(?:json|md)\.cms$"),
                )
                self.assertNotIn(marker, output.read_bytes())
                self.assertNotIn("Confidential", output.name)

    def test_tampered_authenticated_ciphertext_cannot_be_decrypted(self) -> None:
        marker = b"PRIVATE-TRANSCRIPT-MARKER-67f81ee15ed742ff"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, _transcript, _source = self.write_inputs(root)
            result = archive.archive_transcript(
                transcript_path,
                source_path,
                self.certificate,
                root / "archives",
                openssl_binary=OPENSSL,
            )
            tampered_bytes = bytearray(result.json_cms.read_bytes())
            tampered_bytes[-1] ^= 0x01
            tampered_cms = root / "tampered.cms"
            tampered_cms.write_bytes(tampered_bytes)

            final_output = root / "decrypted" / "transcript.json"
            decrypt_result = self.decrypt_to_authenticated_output(
                tampered_cms,
                final_output,
            )

            self.assertNotEqual(decrypt_result.returncode, 0)
            self.assertFalse(final_output.exists())
            self.assertNotIn(marker, decrypt_result.stdout)

    def test_wrong_private_key_cannot_decrypt_archive(self) -> None:
        marker = b"PRIVATE-TRANSCRIPT-MARKER-67f81ee15ed742ff"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, _transcript, _source = self.write_inputs(root)
            result = archive.archive_transcript(
                transcript_path,
                source_path,
                self.certificate,
                root / "archives",
                openssl_binary=OPENSSL,
            )

            final_output = root / "decrypted" / "transcript.json"
            decrypt_result = self.decrypt_to_authenticated_output(
                result.json_cms,
                final_output,
                private_key=self.wrong_private_key,
            )

            self.assertNotEqual(decrypt_result.returncode, 0)
            self.assertFalse(final_output.exists())
            self.assertNotIn(marker, decrypt_result.stdout)

    def test_partial_destination_pair_is_rejected_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, transcript, source = self.write_inputs(root)
            output_dir = root / "archives"
            output_dir.mkdir()
            basename = f"{archive.json_sha256(source)}__{archive.json_sha256(transcript)}"
            json_cms = output_dir / f"{basename}.json.cms"
            markdown_cms = output_dir / f"{basename}.md.cms"
            original_payload = b"existing-file-must-not-be-replaced"
            json_cms.write_bytes(original_payload)

            with self.assertRaisesRegex(archive.ArchiveError, "pair is incomplete"):
                archive.archive_transcript(
                    transcript_path,
                    source_path,
                    self.certificate,
                    output_dir,
                    openssl_binary=OPENSSL,
                )

            self.assertEqual(json_cms.read_bytes(), original_payload)
            self.assertFalse(markdown_cms.exists())
            self.assertEqual(list(output_dir.iterdir()), [json_cms])

    def test_broken_destination_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_path, source_path, transcript, source = self.write_inputs(root)
            output_dir = root / "archives"
            output_dir.mkdir()
            basename = f"{archive.json_sha256(source)}__{archive.json_sha256(transcript)}"
            json_cms = output_dir / f"{basename}.json.cms"
            missing_target = root / "must-not-be-created.cms"
            json_cms.symlink_to(missing_target)

            with self.assertRaisesRegex(archive.ArchiveError, "regular file"):
                archive.archive_transcript(
                    transcript_path,
                    source_path,
                    self.certificate,
                    output_dir,
                    openssl_binary=OPENSSL,
                )

            self.assertTrue(json_cms.is_symlink())
            self.assertFalse(missing_target.exists())
            self.assertEqual(list(output_dir.iterdir()), [json_cms])

    def test_invalid_inputs_write_no_archives(self) -> None:
        invalid_cases = {
            "malformed transcript": ("{not-json", '{"source_url":"https://example.invalid"}'),
            "non-object source": (
                '{"segments":[{"start":0,"end":1,"text":"valid"}]}',
                "[]",
            ),
            "missing segment text": (
                '{"segments":[{"start":0,"end":1}]}',
                '{"source_url":"https://example.invalid"}',
            ),
            "non-finite timestamp": (
                '{"segments":[{"start":NaN,"end":1,"text":"invalid"}]}',
                '{"source_url":"https://example.invalid"}',
            ),
            "unrepresentable timestamp": (
                '{"segments":[{"start":' + ("9" * 400) + ',"end":1,"text":"invalid"}]}',
                '{"source_url":"https://example.invalid"}',
            ),
            "unordered segments": (
                '{"segments":[{"start":2,"end":3,"text":"later"},'
                '{"start":1,"end":2,"text":"earlier"}]}',
                '{"source_url":"https://example.invalid"}',
            ),
        }
        for label, (transcript_json, source_json) in invalid_cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                transcript_path = root / "transcript.json"
                source_path = root / "source.json"
                output_dir = root / "archives"
                transcript_path.write_text(transcript_json, encoding="utf-8")
                source_path.write_text(source_json, encoding="utf-8")

                with self.assertRaises(archive.ArchiveError):
                    archive.archive_transcript(
                        transcript_path,
                        source_path,
                        self.certificate,
                        output_dir,
                        openssl_binary=OPENSSL,
                    )

                self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()

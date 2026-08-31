from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SHOW_WORKFLOW = WORKFLOWS / "daily-china-show.yml"
TOP_WORKFLOW = WORKFLOWS / "daily-top-videos.yml"
ARK_WORKFLOW = WORKFLOWS / "daily-ark-invest-videos.yml"
TRANSCRIPT_CERT = "config/transcript-archive-recipient-v1.pem"


def workflow_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def step_block(workflow: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    start = workflow.find(marker)
    if start < 0:
        raise AssertionError(f"Workflow step is missing: {name}")
    end = workflow.find("\n      - name: ", start + len(marker))
    return workflow[start:] if end < 0 else workflow[start:end]


class EncryptedTranscriptWorkflowTests(unittest.TestCase):
    def test_show_archives_only_after_transcript_duplicate_guard(self) -> None:
        workflow = workflow_text(SHOW_WORKFLOW)
        guard = workflow.index("      - name: Reject repeated show content")
        archive = workflow.index("      - name: Archive encrypted show transcript")
        selection = workflow.index("      - name: Select keynote speakers")

        self.assertLess(guard, archive)
        self.assertLess(archive, selection)

        block = step_block(workflow, "Archive encrypted show transcript")
        self.assertIn("steps.source_guard_transcript.outputs.duplicate != 'true'", block)
        self.assertIn("python tools/archive_encrypted_transcript.py", block)
        self.assertIn("--transcript work/daily/transcript.json", block)
        self.assertIn("--source-metadata work/daily/source_identity.json", block)
        self.assertIn(f"--recipient-cert {TRANSCRIPT_CERT}", block)
        self.assertIn('--output-dir "transcripts/shows/${SHOW_DATE}"', block)

    def test_top_processing_encrypts_before_artifact_and_promotion_is_final(self) -> None:
        workflow = workflow_text(TOP_WORKFLOW)
        processing = step_block(workflow, "Download, transcribe, plan, and render")
        self.assertIn(f"--transcript-recipient-cert {TRANSCRIPT_CERT}", processing)

        process_step = workflow.index("      - name: Download, transcribe, plan, and render")
        upload_step = workflow.index("      - name: Upload top video output")
        self.assertLess(process_step, upload_step)

        evaluate = workflow.index("      - name: Evaluate batch results")
        promote = workflow.index("      - name: Promote encrypted top video transcripts")
        record = workflow.index("      - name: Record successful source identities")
        restore = workflow.index(
            "      - name: Restore existing output when rerun has no successful candidates"
        )
        self.assertLess(evaluate, promote)
        self.assertLess(promote, record)
        self.assertLess(promote, restore)

        promotion = step_block(workflow, "Promote encrypted top video transcripts")
        self.assertIn("python tools/promote_encrypted_transcripts.py", promotion)
        self.assertIn('--staging-root "$OUTPUT_DIR"', promotion)
        self.assertIn('--output-dir "transcripts/top-videos/${RUN_DATE}"', promotion)

    def test_ark_processing_receives_certificate_and_permanent_archive_root(self) -> None:
        workflow = workflow_text(ARK_WORKFLOW)
        processing = step_block(workflow, "Process ARK videos")

        self.assertIn("python tools/process_ark_videos.py", processing)
        self.assertIn(f"--transcript-recipient-cert {TRANSCRIPT_CERT}", processing)
        self.assertIn("--transcript-archive-root transcripts", processing)

    def test_each_commit_verifies_tree_then_stages_only_expected_roots(self) -> None:
        cases = (
            (SHOW_WORKFLOW, "Commit rendered clips to main"),
            (TOP_WORKFLOW, "Commit rendered clips to main"),
            (ARK_WORKFLOW, "Commit rendered clips to main"),
        )
        expected_add = "git add -A -- rendered-clips transcripts"
        verifier = "python tools/verify_encrypted_transcript_tree.py --root transcripts"

        for path, step_name in cases:
            with self.subTest(workflow=path.name):
                workflow = workflow_text(path)
                block = step_block(workflow, step_name)
                self.assertIn(verifier, block)
                self.assertIn(expected_add, block)
                self.assertLess(block.index(verifier), block.index(expected_add))

                git_add_lines = [
                    line.strip()
                    for line in workflow.splitlines()
                    if line.strip().startswith("git add ")
                ]
                self.assertEqual(git_add_lines, [expected_add])

    def test_rendered_clip_cleanup_never_targets_permanent_transcripts(self) -> None:
        for path in (SHOW_WORKFLOW, TOP_WORKFLOW, ARK_WORKFLOW):
            with self.subTest(workflow=path.name):
                workflow = workflow_text(path)
                self.assertNotIn("cleanup_rendered_clips.py --target transcripts", workflow)
                self.assertNotIn("cleanup_rendered_clips.py --target=transcripts", workflow)
                for line in workflow.splitlines():
                    if "cleanup_rendered_clips.py" in line:
                        self.assertNotIn("transcripts", line)

    def test_workflows_are_valid_yaml_when_pyyaml_is_available(self) -> None:
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML is not installed")

        for path in (SHOW_WORKFLOW, TOP_WORKFLOW, ARK_WORKFLOW):
            with self.subTest(workflow=path.name):
                payload = yaml.safe_load(workflow_text(path))
                self.assertIsInstance(payload, dict)
                self.assertIn("jobs", payload)


if __name__ == "__main__":
    unittest.main()

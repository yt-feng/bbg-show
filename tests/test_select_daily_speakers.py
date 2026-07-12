from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import select_daily_speakers as selector  # noqa: E402


class SelectDailySpeakersTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.transcript = root / "transcript.json"
        self.output = root / "speakers.json"
        self.transcript.write_text(
            json.dumps(
                {
                    "segments": [
                        {
                            "start": 0.0,
                            "end": 120.0,
                            "text": "A substantive guest interview.",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def invoke_main(
        self,
        candidates: list[selector.Candidate],
        *extra_args: str,
    ) -> None:
        argv = [
            "select_daily_speakers.py",
            "--transcript",
            str(self.transcript),
            "--show-date",
            "2026-05-10",
            "--out",
            str(self.output),
            *extra_args,
        ]
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(sys, "argv", argv),
            patch.object(selector, "find_candidates_in_window", return_value=candidates),
        ):
            selector.main()

    def read_output(self) -> dict[str, object]:
        return json.loads(self.output.read_text(encoding="utf-8"))

    def test_empty_selection_is_strict_by_default(self) -> None:
        with self.assertRaisesRegex(SystemExit, "No keynote speakers selected"):
            self.invoke_main([])

        payload = self.read_output()
        self.assertEqual(payload["selection_status"], "no_eligible_speakers")
        self.assertEqual(payload["speakers"], [])

    def test_allow_empty_returns_successfully(self) -> None:
        self.invoke_main([], "--allow-empty")

        payload = self.read_output()
        self.assertEqual(payload["selection_status"], "no_eligible_speakers")
        self.assertEqual(payload["speakers"], [])

    def test_selected_payload_has_selected_status(self) -> None:
        candidate = selector.Candidate(
            speaker="Jane Doe",
            context="Acme CEO",
            start=10.0,
            end=100.0,
            confidence=0.9,
            importance=0.8,
            reason="Substantive business interview",
        )

        self.invoke_main([candidate])

        payload = self.read_output()
        self.assertEqual(payload["selection_status"], "selected")
        self.assertEqual(len(payload["speakers"]), 1)

    def test_sensitive_transition_does_not_remove_discovery_candidate(self) -> None:
        segments = [
            {
                "start": 0.0,
                "end": 15.0,
                "text": "A transition mentions President Trump before the interview.",
            },
            {
                "start": 15.0,
                "end": 120.0,
                "text": "Jane Doe discusses Acme's product strategy and earnings.",
            },
        ]
        deepseek_result = {
            "candidates": [
                {
                    "speaker": "Jane Doe",
                    "context": "Acme CEO",
                    "start": 0.0,
                    "end": 120.0,
                    "confidence": 0.9,
                    "importance": 0.8,
                    "reason": "Substantive business interview",
                }
            ]
        }

        with patch.object(selector, "ask_deepseek", return_value=deepseek_result) as ask:
            candidates = selector.find_candidates_in_window("test-key", segments, 0.0, 120.0)

        self.assertEqual([candidate.speaker for candidate in candidates], ["Jane Doe"])
        user_prompt = ask.call_args.args[2]
        self.assertNotIn("Any segment about sensitive geopolitics", user_prompt)

    def test_anchor_and_short_candidates_are_still_filtered(self) -> None:
        deepseek_result = {
            "candidates": [
                {
                    "speaker": "Yvonne Man",
                    "context": "Bloomberg host",
                    "start": 0.0,
                    "end": 120.0,
                },
                {
                    "speaker": "Short Guest",
                    "context": "Analyst",
                    "start": 0.0,
                    "end": 59.0,
                },
                {
                    "speaker": "Valid Guest",
                    "context": "Economist",
                    "start": 20.0,
                    "end": 100.0,
                },
            ]
        }
        segments = [{"start": 0.0, "end": 120.0, "text": "Interview transcript"}]

        with patch.object(selector, "ask_deepseek", return_value=deepseek_result):
            candidates = selector.find_candidates_in_window("test-key", segments, 0.0, 120.0)

        self.assertEqual([candidate.speaker for candidate in candidates], ["Valid Guest"])

    def test_max_speakers_must_be_positive(self) -> None:
        with self.assertRaisesRegex(SystemExit, "--max-speakers must be at least 1"):
            self.invoke_main([], "--max-speakers", "0")

        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()

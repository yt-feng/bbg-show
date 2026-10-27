from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))

import deepseek_api  # noqa: E402
import plan_speaker_full  # noqa: E402
import plan_speaker_highlights  # noqa: E402


class FakeResponse:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        # A truncated content string still arrives inside valid HTTP JSON.
        return json.dumps({"choices": [{"finish_reason": "length", "message": {
            "content": '{"clips": [{"title": "unfinished',
        }}]}).encode()


class SpeakerPlannerTests(unittest.TestCase):
    def run_main(self, root: Path) -> Path:
        transcript = root / "transcript.json"
        transcript.write_text(json.dumps({"segments": [
            {"start": index * 5, "end": (index + 1) * 5, "text": f"Source statement {index}."}
            for index in range(12)
        ]}), encoding="utf-8")
        output = root / "highlight_plan.json"
        with mock.patch.object(sys, "argv", [
            "plan_speaker_highlights.py", "--transcript", str(transcript),
            "--speaker", "Guest", "--segment-start", "0", "--segment-end", "60",
            "--min-seconds", "30", "--max-seconds", "150", "--max-clips", "5",
            "--out", str(output),
        ]):
            plan_speaker_highlights.main()
        return output

    def test_selection_and_batched_translation_preserve_source_text_and_times(self) -> None:
        """The model never needs to return a multi-clip bilingual JSON document."""
        translated_batches: list[list[dict]] = []

        def translate(_key, _system, prompt, **_kwargs):
            raw = prompt.split("Translate these subtitle units:\n", 1)[1]
            units, _ = json.JSONDecoder().raw_decode(raw)
            translated_batches.append(units)
            return {"items": [
                {"index": unit["index"], "zh": f"中文内容{unit['index']}",
                 "zh_highlights": ["中文"], "en_highlights": ["Source"]}
                for unit in units
            ]}

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(plan_speaker_highlights, "ask_deepseek", return_value={"clips": [{
                "start": 0, "end": 60, "title": "嘉宾观点", "title_lines": ["嘉宾", "市场", "观点"],
            }]}) as select,
            mock.patch.object(plan_speaker_full, "ask_deepseek", side_effect=translate),
            mock.patch.object(plan_speaker_highlights, "remove_trump_clips_from_plan", return_value=[]),
        ):
            output = self.run_main(Path(tmp))
            plan = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual([len(batch) for batch in translated_batches], [10, 2])
        self.assertNotIn('"subtitles":', select.call_args.args[2])
        subtitles = plan["clips"][0]["subtitles"]
        self.assertEqual(len(subtitles), 12)
        for index, subtitle in enumerate(subtitles):
            self.assertEqual(subtitle["en"], f"Source statement {index}.")
            self.assertEqual((subtitle["start"], subtitle["end"]), (index * 5, (index + 1) * 5))
            self.assertEqual((subtitle["relative_start"], subtitle["relative_end"]), (index * 5, (index + 1) * 5))
            self.assertEqual(subtitle["zh"], f"中文内容{index + 1}")

    def test_truncated_json_cannot_become_success_or_content_exclusion(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(deepseek_api, "urlopen", return_value=FakeResponse()) as request,
            mock.patch.object(deepseek_api.time, "sleep"),
            mock.patch.object(plan_speaker_highlights, "build_transcript_fallback_clip") as fallback,
        ):
            root = Path(tmp)
            with self.assertRaisesRegex(SystemExit, "DeepSeek API failed") as raised:
                self.run_main(root)
            self.assertNotEqual(raised.exception.code, plan_speaker_highlights.NO_ELIGIBLE_CLIPS_EXIT_CODE)
            self.assertFalse((root / "highlight_plan.json").exists())
        self.assertEqual(request.call_count, 3)
        fallback.assert_not_called()

    def test_explicit_no_eligible_clips_does_not_invent_a_fallback(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(plan_speaker_highlights, "ask_deepseek", return_value={"clips": []}),
            mock.patch.object(plan_speaker_highlights, "build_transcript_fallback_clip") as fallback,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(Path(tmp))
        self.assertEqual(raised.exception.code, plan_speaker_highlights.NO_ELIGIBLE_CLIPS_EXIT_CODE)
        fallback.assert_not_called()

    def test_missing_clips_field_is_an_error_not_an_excluded_segment(self) -> None:
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(plan_speaker_highlights, "ask_deepseek", return_value={}),
        ):
            with self.assertRaisesRegex(SystemExit, "must contain a clips list"):
                self.run_main(Path(tmp))

    def test_content_filter_exclusion_remains_a_legitimate_empty_outcome(self) -> None:
        def exclude(plan, **_kwargs):
            removed = list(plan["clips"])
            plan["clips"] = []
            return removed

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(plan_speaker_highlights, "ask_deepseek", return_value={"clips": [{
                "start": 0, "end": 60, "title": "Excluded topic",
            }]}),
            mock.patch.object(plan_speaker_highlights, "remove_trump_clips_from_plan", side_effect=exclude),
            mock.patch.object(plan_speaker_full, "ask_deepseek") as translate,
        ):
            with self.assertRaises(SystemExit) as raised:
                self.run_main(Path(tmp))
        self.assertEqual(raised.exception.code, plan_speaker_highlights.NO_ELIGIBLE_CLIPS_EXIT_CODE)
        translate.assert_not_called()

    def test_missing_translation_fails_after_bounded_missing_row_retries(self) -> None:
        clips = [{"subtitles": [{"start": 10, "end": 15, "en": "Original sentence."}]}]
        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(plan_speaker_full, "ask_deepseek", return_value={"items": []}) as translate,
        ):
            with self.assertRaisesRegex(SystemExit, "Missing Chinese translation for clip 1 subtitle 1"):
                plan_speaker_highlights.translate_clip_subtitles(clips, "Guest", "")
        self.assertEqual(translate.call_count, 4)

    def test_null_and_non_string_translations_are_retried_then_fail(self) -> None:
        for invalid_zh in (None, 42, True, [], {}, " \n "):
            with self.subTest(zh=invalid_zh):
                clips = [{"subtitles": [{"start": 10, "end": 15, "en": "Original sentence."}]}]
                with (
                    mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
                    mock.patch.object(plan_speaker_full, "ask_deepseek", return_value={
                        "items": [{"index": 1, "zh": invalid_zh}],
                    }) as translate,
                ):
                    with self.assertRaisesRegex(SystemExit, "Missing Chinese translation for clip 1 subtitle 1"):
                        plan_speaker_highlights.translate_clip_subtitles(clips, "Guest", "")
                self.assertEqual(translate.call_count, 4)
                self.assertNotIn("zh", clips[0]["subtitles"][0])

    def test_null_translation_is_replaced_by_successful_missing_row_retry(self) -> None:
        clips = [{"subtitles": [{"start": 10, "end": 15, "en": "Original sentence."}]}]
        with (
            mock.patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            mock.patch.object(plan_speaker_full, "ask_deepseek", side_effect=[
                {"items": [{"index": 1, "zh": None}]},
                {"items": [{"index": 1, "zh": "原始句子。"}]},
            ]) as translate,
        ):
            plan_speaker_highlights.translate_clip_subtitles(clips, "Guest", "")
        self.assertEqual(translate.call_count, 2)
        self.assertEqual(clips[0]["subtitles"][0]["zh"], "原始句子。")


if __name__ == "__main__":
    unittest.main()

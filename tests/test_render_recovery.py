from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zlib


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import render_clips_linux as renderer


class RenderRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / "clip.mp4"
        self.clip = {"title": "Test clip", "start": 0, "end": 60, "subtitles": []}

    def render(self) -> None:
        renderer.render_clip(
            source=self.root / "source.mp4", clip=self.clip,
            static_png=self.root / "static.png", timed_overlays=[],
            output=self.output, threads=2,
        )

    def test_timeout_retries_once_after_removing_partial_file(self) -> None:
        calls = []

        def run(command, **kwargs):
            self.assertFalse(self.output.exists())
            calls.append(kwargs)
            self.output.write_bytes(b"partial" if len(calls) == 1 else b"complete")
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(command, 0)

        with patch.object(renderer.subprocess, "run", side_effect=run):
            renderer.run_render_command(["ffmpeg"], stage="test", duration=60, outputs=(self.output,))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["timeout"] == 480 for call in calls))
        self.assertEqual(self.output.read_bytes(), b"complete")

    def test_second_timeout_fails_and_removes_partial_file(self) -> None:
        def run(command, **kwargs):
            self.output.write_bytes(b"partial")
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])

        with patch.object(renderer.subprocess, "run", side_effect=run) as child:
            with self.assertRaisesRegex(renderer.RenderTimeoutError, "timed out twice"):
                renderer.run_render_command(["ffmpeg"], stage="piece", duration=60, outputs=(self.output,))
        self.assertEqual(child.call_count, 2)
        self.assertFalse(self.output.exists())

    def test_non_timeout_failure_is_not_retried_and_removes_partial_file(self) -> None:
        def run(command, **kwargs):
            self.output.write_bytes(b"partial")
            raise subprocess.CalledProcessError(1, command)

        with patch.object(renderer.subprocess, "run", side_effect=run) as child:
            with self.assertRaises(subprocess.CalledProcessError):
                renderer.run_render_command(["ffmpeg"], stage="piece", duration=60, outputs=(self.output,))
        child.assert_called_once()
        self.assertFalse(self.output.exists())

    def test_invalid_render_never_replaces_previous_published_file(self) -> None:
        self.output.write_bytes(b"previous-complete")

        def write_partial(**kwargs):
            self.assertNotEqual(kwargs["output"], self.output)
            kwargs["output"].write_bytes(b"new-incomplete")

        with patch.object(renderer, "_render_clip_to_file", side_effect=write_partial), patch.object(
            renderer, "validate_video", side_effect=renderer.RenderError("truncated")
        ):
            with self.assertRaisesRegex(renderer.RenderError, "truncated"):
                self.render()
        self.assertEqual(self.output.read_bytes(), b"previous-complete")
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def test_validated_render_is_published_only_after_probe(self) -> None:
        def write_video(**kwargs):
            kwargs["output"].write_bytes(b"complete")

        def validate(path, duration):
            self.assertFalse(self.output.exists())
            self.assertEqual(path.read_bytes(), b"complete")
            self.assertEqual(duration, 60)

        with patch.object(renderer, "_render_clip_to_file", side_effect=write_video), patch.object(
            renderer, "validate_video", side_effect=validate
        ):
            self.render()
        self.assertEqual(self.output.read_bytes(), b"complete")
        self.assertEqual(list(self.root.iterdir()), [self.output])

    def probe_payload(self, *, video_duration=60, audio_duration=60, audio_packets="100"):
        return {"format": {"duration": "60"}, "streams": [
            {"codec_type": "video", "duration": str(video_duration), "width": 1080, "height": 1920, "nb_read_packets": "1800"},
            {"codec_type": "audio", "duration": str(audio_duration), "nb_read_packets": audio_packets},
        ]}

    def test_probe_rejects_short_audio_missing_packets_and_demux_errors(self) -> None:
        self.output.write_bytes(b"media")
        for payload, stderr in (
            (self.probe_payload(video_duration=5), ""),
            (self.probe_payload(audio_duration=5), ""),
            (self.probe_payload(audio_packets="0"), ""),
            (self.probe_payload(), "partial file"),
        ):
            with self.subTest(payload=payload, stderr=stderr), patch.object(
                renderer, "run_render_command", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), stderr)
            ):
                with self.assertRaises(renderer.RenderError):
                    renderer.validate_video(self.output, 60)

    def test_probe_accepts_complete_audio_and_video(self) -> None:
        self.output.write_bytes(b"media")
        with patch.object(renderer, "run_render_command", return_value=subprocess.CompletedProcess(
            [], 0, json.dumps(self.probe_payload()), ""
        )):
            renderer.validate_video(self.output, 60)

    def main_arguments(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"clips": [self.clip]}), encoding="utf-8")
        out_dir = self.root / "output"
        out_dir.mkdir()
        self.output = out_dir / renderer.output_name(1, self.clip)
        self.output.write_bytes(b"old-file")
        return ["render_clips_linux.py", "--source", str(source), "--plan", str(plan),
                "--out-dir", str(out_dir), "--work-dir", str(self.root / "work")]

    def test_bad_existing_video_is_rendered_again_instead_of_skipped(self) -> None:
        argv = self.main_arguments()

        def write_video(**kwargs):
            self.assertFalse(kwargs["output"].exists())
            kwargs["output"].write_bytes(b"replacement")

        with patch.object(sys, "argv", argv), patch.object(
            renderer, "validate_video", side_effect=renderer.RenderError("truncated")
        ), patch.object(renderer, "render_overlay_images", return_value=(self.root / "static.png", [])), patch.object(
            renderer, "render_clip", side_effect=write_video
        ) as render:
            renderer.main()
        render.assert_called_once()
        self.assertEqual(self.output.read_bytes(), b"replacement")

    def test_probe_timeout_preserves_existing_video_and_does_not_rerender(self) -> None:
        argv = self.main_arguments()
        with patch.object(sys, "argv", argv), patch.object(
            renderer, "validate_video", side_effect=renderer.RenderTimeoutError("probe timeout")
        ), patch.object(renderer, "render_clip") as render:
            with self.assertRaises(renderer.RenderTimeoutError):
                renderer.main()
        render.assert_not_called()
        self.assertEqual(self.output.read_bytes(), b"old-file")

    def test_one_hundredth_second_tail_is_merged_without_dropping_overlays(self) -> None:
        # Production failure: the fourth group covers only 90.99-91.00s.
        boundaries = [0, 6, 12, 18, 25.85, 35, 45, 55, 61.81, 70, 78, 85, 90.99, 91]
        overlays = [
            (self.root / f"overlay_{index}_{y}.png", boundaries[index], boundaries[index + 1], y)
            for index in range(13)
            for y in (renderer.CAPTION_Y, renderer.COMMENT_Y)
        ]
        pieces = renderer.build_render_pieces(91, overlays)
        self.assertEqual([(start, end) for start, end, _ in pieces], [(0, 25.85), (25.85, 61.81), (61.81, 91)])
        self.assertEqual([overlay for _, _, group in pieces for overlay in group], overlays)
        self.assertEqual(pieces[-1][2][-2:], overlays[-2:])

    def test_short_first_and_middle_intervals_are_merged_without_gaps(self) -> None:
        for starts in ([0, 0.01, 1, 2], [0, 1, 1.01, 2]):
            overlays = [
                (self.root / f"overlay_{index}.png", starts[index // 8], starts[index // 8 + 1], renderer.CAPTION_Y)
                for index in range(24)
            ]
            with self.subTest(starts=starts):
                pieces = renderer.build_render_pieces(2, overlays)
                self.assertEqual([(start, end) for start, end, _ in pieces], [(0, starts[2]), (starts[2], 2)])
                self.assertEqual([overlay for _, _, group in pieces for overlay in group], overlays)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
    def test_short_synthetic_video_renders_and_passes_real_probe(self) -> None:
        source = self.root / "source.mp4"
        static_png = self.root / "static.png"

        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

        static_png.write_bytes(
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1080, 1920, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * (1080 * 4)) * 1920))
            + chunk(b"IEND", b"")
        )
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x480:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "1.3", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(source),
        ], check=True, timeout=30)
        renderer.render_clip(
            source=source, clip={"start": 0, "end": 0.6}, static_png=static_png,
            timed_overlays=[], output=self.output, threads=2,
        )
        renderer.validate_video(self.output, 0.6)
        self.assertGreater(self.output.stat().st_size, 0)
        self.assertFalse(list(self.root.glob(".*.rendering.mp4")))
        # Exercise the independently bounded piece, concat, audio, and mux
        # stages through the normal public renderer entry point.
        overlays = [
            (static_png, index / 10, (index + 1) / 10, y)
            for index in range(13)
            for y in (renderer.CAPTION_Y, renderer.COMMENT_Y)
        ]
        renderer.render_clip(
            source=source, clip={"start": 0, "end": 1.3}, static_png=static_png,
            timed_overlays=overlays, output=self.output, threads=2,
        )
        renderer.validate_video(self.output, 1.3)
        self.assertEqual(len(list((self.root / "pieces").glob("part_*.mp4"))), 4)
        self.assertFalse(list(self.root.glob(".*.rendering.mp4")))
        truncated = self.root / "truncated.mp4"
        encoded = self.output.read_bytes()
        truncated.write_bytes(encoded[:len(encoded) // 2])
        with self.assertRaises((renderer.RenderError, subprocess.CalledProcessError)):
            renderer.validate_video(truncated, 1.3)
        # A source offset between 30fps frames leaves the final 0.01s interval
        # with no source frame. The unmerged command hangs; exercise the fixed
        # partition through real ffmpeg with a hard test deadline per stage.
        boundaries = [index / 12 for index in range(13)] + [1.01]
        tail_overlays = [
            (static_png, boundaries[index], boundaries[index + 1], y)
            for index in range(13)
            for y in (renderer.CAPTION_Y, renderer.COMMENT_Y)
        ]
        with patch.object(renderer, "stage_timeout", return_value=10):
            renderer.render_clip(
                source=source, clip={"start": 0.01, "end": 1.02}, static_png=static_png,
                timed_overlays=tail_overlays, output=self.output, threads=2,
            )
        renderer.validate_video(self.output, 1.01)


if __name__ == "__main__":
    unittest.main()

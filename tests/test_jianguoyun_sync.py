from __future__ import annotations

import argparse
import hashlib
import io
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request


ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import sync_rendered_clips_to_jianguoyun as sync  # noqa: E402


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    timeout: int


class FakeResponse:
    def __init__(
        self,
        status: int,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> None:
        self.status = status
        self.headers = dict(headers or {})
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        return self.body


class RecordingOpener:
    def __init__(
        self,
        handler: Callable[[RecordedRequest], FakeResponse],
    ) -> None:
        self.handler = handler
        self.calls: list[RecordedRequest] = []

    def __call__(self, request: Request, *, timeout: int) -> FakeResponse:
        data = request.data
        if data is None:
            body = b""
        elif isinstance(data, bytes):
            body = data
        else:
            body = data.read()
        call = RecordedRequest(
            method=request.get_method(),
            url=request.full_url,
            headers={key.lower(): value for key, value in request.header_items()},
            body=body,
            timeout=timeout,
        )
        self.calls.append(call)
        return self.handler(call)


def http_error(call: RecordedRequest, status: int) -> HTTPError:
    return HTTPError(
        call.url,
        status,
        f"HTTP {status}",
        hdrs={},
        fp=io.BytesIO(b""),
    )


def git_blob_sha(payload: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(f"blob {len(payload)}\0".encode("ascii"))
    digest.update(payload)
    return digest.hexdigest()


class JianguoyunMappingTests(unittest.TestCase):
    def test_show_uses_next_date_and_top_videos_keeps_run_date(self) -> None:
        self.assertEqual(sync.remote_date_for("show", "2026-07-24"), "2026-07-25")
        self.assertEqual(sync.remote_date_for("show", "2026-12-31"), "2027-01-01")
        self.assertEqual(sync.remote_date_for("top-videos", "2026-07-25"), "2026-07-25")
        self.assertEqual(sync.KIND_CATEGORIES["show"], "BBG Show")
        self.assertEqual(sync.KIND_CATEGORIES["top-videos"], "BBG Top Videos")

    def test_remote_url_percent_encodes_each_path_segment(self) -> None:
        target = sync.WebDavTarget(
            "https://dav.jianguoyun.com/dav/",
            "user@example.com",
            "application-password",
        )
        parts = [
            "我的坚果云",
            "KC Desk Notes",
            "Ops",
            "2026-07-25",
            "BBG Show",
            "01_人名 AI?/片段.mp4",
        ]

        url = target.url(parts)

        expected = "https://dav.jianguoyun.com/dav/" + "/".join(
            quote(part, safe="") for part in parts
        )
        self.assertEqual(url, expected)
        self.assertIn("BBG%20Show", url)
        self.assertIn("%3F%2F", url)
        self.assertNotIn("application-password", url)


class JianguoyunWebDavTests(unittest.TestCase):
    def target(self, opener: RecordingOpener) -> sync.WebDavTarget:
        return sync.WebDavTarget(
            "https://dav.jianguoyun.com/dav/",
            "user@example.com",
            "application-password",
            opener=opener,
            sleeper=lambda _seconds: None,
        )

    def test_mkcol_creates_every_collection_and_accepts_existing_parent(self) -> None:
        def handler(call: RecordedRequest) -> FakeResponse:
            if call.method != "MKCOL":
                self.fail(f"Unexpected method: {call.method}")
            if call.url.endswith("/%E6%88%91%E7%9A%84%E5%9D%9A%E6%9E%9C%E4%BA%91/KC%20Desk%20Notes"):
                raise http_error(call, 405)
            return FakeResponse(201)

        opener = RecordingOpener(handler)
        self.target(opener).ensure_collection(["2026-07-25", "BBG Show"])

        expected_parts = [
            ["我的坚果云"],
            ["我的坚果云", "KC Desk Notes"],
            ["我的坚果云", "KC Desk Notes", "Ops"],
            ["我的坚果云", "KC Desk Notes", "Ops", "2026-07-25"],
            ["我的坚果云", "KC Desk Notes", "Ops", "2026-07-25", "BBG Show"],
        ]
        self.assertEqual([call.method for call in opener.calls], ["MKCOL"] * 5)
        self.assertEqual(
            [call.url for call in opener.calls],
            [
                "https://dav.jianguoyun.com/dav/"
                + "/".join(quote(part, safe="") for part in parts)
                for parts in expected_parts
            ],
        )

    def test_same_remote_size_skips_put(self) -> None:
        payload = b"already uploaded"

        def handler(call: RecordedRequest) -> FakeResponse:
            if call.method == "MKCOL":
                return FakeResponse(201)
            if call.method == "HEAD":
                return FakeResponse(200, headers={"Content-Length": str(len(payload))})
            self.fail(f"Existing same-size file must not use {call.method}")

        opener = RecordingOpener(handler)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(payload)
            uploaded, skipped, failed = sync.sync_files(
                self.target(opener),
                files=[clip],
                kind="show",
                remote_date="2026-07-25",
            )

        self.assertEqual((uploaded, skipped, failed), (0, 1, 0))
        self.assertEqual([call.method for call in opener.calls].count("HEAD"), 1)
        self.assertNotIn("PUT", [call.method for call in opener.calls])

    def test_missing_file_is_put_then_verified_with_a_second_head(self) -> None:
        payload = b"new rendered clip"
        uploaded_remotely = False

        def handler(call: RecordedRequest) -> FakeResponse:
            nonlocal uploaded_remotely
            if call.method == "MKCOL":
                return FakeResponse(201)
            if call.method == "HEAD":
                if not uploaded_remotely:
                    raise http_error(call, 404)
                return FakeResponse(200, headers={"Content-Length": str(len(payload))})
            if call.method == "PUT":
                self.assertEqual(call.body, payload)
                self.assertEqual(call.headers["content-type"], "video/mp4")
                self.assertEqual(call.headers["content-length"], str(len(payload)))
                uploaded_remotely = True
                return FakeResponse(201)
            self.fail(f"Unexpected method: {call.method}")

        opener = RecordingOpener(handler)
        with tempfile.TemporaryDirectory() as tmp:
            clip = Path(tmp) / "clip.mp4"
            clip.write_bytes(payload)
            uploaded, skipped, failed = sync.sync_files(
                self.target(opener),
                files=[clip],
                kind="top-videos",
                remote_date="2026-07-25",
            )

        self.assertEqual((uploaded, skipped, failed), (1, 0, 0))
        file_calls = [
            call.method for call in opener.calls if call.method in {"HEAD", "PUT"}
        ]
        self.assertEqual(file_calls, ["HEAD", "PUT", "HEAD"])
        self.assertTrue(uploaded_remotely)

    def test_duplicate_basenames_get_stable_git_blob_hash_suffixes(self) -> None:
        first_payload = b"first clip"
        second_payload = b"second clip"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / "clip.mp4"
            second = root / "two" / "clip.mp4"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(first_payload)
            second.write_bytes(second_payload)

            names = sync.assign_remote_names([first, second])

        self.assertEqual(
            names[first],
            f"clip__{git_blob_sha(first_payload)[:8]}.mp4",
        )
        self.assertEqual(
            names[second],
            f"clip__{git_blob_sha(second_payload)[:8]}.mp4",
        )
        self.assertNotEqual(names[first], names[second])

    def test_long_duplicate_names_keep_their_hash_suffixes(self) -> None:
        first_payload = b"first clip"
        second_payload = b"second clip"
        long_name = f"{'非常长的标题' * 30}.mp4"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "one" / long_name
            second = root / "two" / long_name
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(first_payload)
            second.write_bytes(second_payload)

            names = sync.assign_remote_names([first, second])

        self.assertLessEqual(
            len(names[first].encode("utf-8")),
            sync.MAX_REMOTE_FILENAME_BYTES,
        )
        self.assertIn(f"__{git_blob_sha(first_payload)[:8]}.mp4", names[first])
        self.assertIn(f"__{git_blob_sha(second_payload)[:8]}.mp4", names[second])
        self.assertNotEqual(names[first], names[second])

    def test_head_without_content_length_falls_back_to_propfind(self) -> None:
        propfind_body = b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>
<d:getcontentlength>1234</d:getcontentlength>
</d:prop></d:propstat></d:response></d:multistatus>"""

        def handler(call: RecordedRequest) -> FakeResponse:
            if call.method == "HEAD":
                return FakeResponse(200)
            if call.method == "PROPFIND":
                self.assertEqual(call.headers["depth"], "0")
                return FakeResponse(207, body=propfind_body)
            self.fail(f"Unexpected method: {call.method}")

        opener = RecordingOpener(handler)
        size = self.target(opener).remote_size(
            ["2026-07-25", "BBG Show", "clip.mp4"],
        )

        self.assertEqual(size, 1234)
        self.assertEqual(
            [call.method for call in opener.calls],
            ["HEAD", "PROPFIND"],
        )

    def test_invalid_head_length_and_204_fall_back_to_propfind(self) -> None:
        for head_status, head_length in (
            (200, "not-a-number"),
            (200, "0"),
            (204, "0"),
        ):
            with self.subTest(head_status=head_status, head_length=head_length):
                propfind_body = b"""<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>
<d:getcontentlength>4321</d:getcontentlength>
</d:prop></d:propstat></d:response></d:multistatus>"""

                def handler(call: RecordedRequest) -> FakeResponse:
                    if call.method == "HEAD":
                        return FakeResponse(
                            head_status,
                            headers={"Content-Length": head_length},
                        )
                    if call.method == "PROPFIND":
                        return FakeResponse(207, body=propfind_body)
                    self.fail(f"Unexpected method: {call.method}")

                opener = RecordingOpener(handler)
                size = self.target(opener).remote_size(
                    ["2026-07-25", "BBG Show", "clip.mp4"],
                    expected_size=4321,
                )

                self.assertEqual(size, 4321)
                self.assertEqual(
                    [call.method for call in opener.calls],
                    ["HEAD", "PROPFIND"],
                )

    def test_one_file_failure_does_not_block_later_uploads(self) -> None:
        class PartiallyFailingTarget:
            def __init__(self) -> None:
                self.uploaded: set[str] = set()
                self.sleeper = lambda _seconds: None

            def ensure_collection(self, _parts) -> None:
                return None

            def remote_size(
                self,
                parts: list[str],
                *,
                expected_size: int | None = None,
            ) -> int | None:
                name = parts[-1]
                if name == "first.mp4":
                    raise sync.JianguoyunSyncError("simulated HEAD failure")
                return expected_size if name in self.uploaded else None

            def upload(self, _path: Path, parts: list[str]) -> None:
                self.uploaded.add(parts[-1])

        target = PartiallyFailingTarget()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.mp4"
            second = root / "second.mp4"
            first.write_bytes(b"first!")
            second.write_bytes(b"second")

            uploaded, skipped, failed = sync.sync_files(
                target,  # type: ignore[arg-type]
                files=[first, second],
                kind="show",
                remote_date="2026-07-25",
            )

        self.assertEqual((uploaded, skipped, failed), (1, 0, 1))
        self.assertEqual(target.uploaded, {"second.mp4"})

    def test_allow_empty_returns_before_reading_webdav_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = argparse.Namespace(
                kind="show",
                date="2026-07-24",
                source_dir=tmp,
                remote_root=sync.DEFAULT_REMOTE_ROOT,
                allow_empty=True,
            )
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    sync,
                    "require_env",
                    side_effect=AssertionError("secrets must not be read"),
                ) as require_env,
            ):
                result = sync.run(args)

        self.assertEqual(result, 0)
        require_env.assert_not_called()


class JianguoyunWorkflowWiringTests(unittest.TestCase):
    SECRET_EXPRESSIONS = (
        "${{ secrets.JIANGUOYUN_WEBDAV_URL }}",
        "${{ secrets.JIANGUOYUN_WEBDAV_USERNAME }}",
        "${{ secrets.JIANGUOYUN_WEBDAV_PASSWORD }}",
    )
    SCRIPT = "tools/sync_rendered_clips_to_jianguoyun.py"

    def workflow(self, name: str) -> str:
        path = ROOT / ".github" / "workflows" / name
        self.assertTrue(path.is_file(), f"Missing workflow: {path}")
        return path.read_text(encoding="utf-8")

    def assert_secrets_are_wired(self, workflow: str) -> None:
        for expression in self.SECRET_EXPRESSIONS:
            with self.subTest(secret=expression):
                self.assertIn(expression, workflow)

    def upload_step(self, workflow: str) -> str:
        command_index = workflow.index(self.SCRIPT)
        step_start = workflow.rfind("      - name:", 0, command_index)
        self.assertGreaterEqual(step_start, 0)
        next_step = workflow.find("\n      - name:", command_index)
        return workflow[step_start:] if next_step < 0 else workflow[step_start:next_step]

    def test_daily_show_uploads_after_commit_even_when_render_was_skipped(self) -> None:
        workflow = self.workflow("daily-china-show.yml")
        command_index = workflow.index(self.SCRIPT)

        self.assertGreater(
            command_index,
            workflow.index("- name: Commit rendered clips to main"),
        )
        step = self.upload_step(workflow)
        self.assertIn("--kind show", step)
        self.assertRegex(step, r'--date\s+"\$\{?SHOW_DATE\}?"')
        self.assertRegex(step, r'--source-dir\s+"\$\{?OUTPUT_DIR\}?"')
        self.assertIn("--allow-empty", step)
        self.assertNotIn("existing_clips.outputs.skip != 'true'", step)
        self.assert_secrets_are_wired(step)

    def test_top_videos_uploads_after_commit(self) -> None:
        workflow = self.workflow("daily-top-videos.yml")
        command_index = workflow.index(self.SCRIPT)

        self.assertGreater(
            command_index,
            workflow.index("- name: Commit rendered clips to main"),
        )
        step = self.upload_step(workflow)
        self.assertIn("--kind top-videos", step)
        self.assertRegex(step, r'--date\s+"\$\{?RUN_DATE\}?"')
        self.assertRegex(step, r'--source-dir\s+"\$\{?OUTPUT_DIR\}?"')
        self.assertIn("--allow-empty", step)
        self.assert_secrets_are_wired(step)

    def test_manual_backfill_workflow_maps_delivery_date_and_runs_both_sync_kinds(self) -> None:
        workflow = self.workflow("sync-jianguoyun.yml")

        self.assertIn("workflow_dispatch:", workflow)
        self.assertRegex(workflow, r"(?m)^\s+kind:\s*$")
        self.assertRegex(workflow, r"(?m)^\s+target_date:\s*$")
        self.assertIn("show", workflow)
        self.assertIn("top-videos", workflow)
        self.assertEqual(workflow.count(self.SCRIPT), 2)
        self.assertIn("target_date - timedelta(days=1)", workflow)
        self.assertIn("--kind show", workflow)
        self.assertRegex(workflow, r'--date\s+"\$SHOW_DATE"')
        self.assertRegex(workflow, r'--source-dir\s+"rendered-clips/\$SHOW_DATE"')
        self.assertIn("--kind top-videos", workflow)
        self.assertRegex(workflow, r'--date\s+"\$TARGET_DATE"')
        self.assertRegex(
            workflow,
            r'--source-dir\s+"rendered-clips/top-videos/\$TARGET_DATE"',
        )
        self.assertNotIn("continue-on-error", workflow)
        self.assert_secrets_are_wired(workflow)


if __name__ == "__main__":
    unittest.main()

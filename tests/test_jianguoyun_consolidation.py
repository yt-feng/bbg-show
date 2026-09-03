from __future__ import annotations

import contextlib
import hashlib
import io
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, unquote, urlparse


TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS_DIR))
import consolidate_jianguoyun_folders as migration  # noqa: E402
from sync_rendered_clips_to_jianguoyun import JianguoyunSyncError, WebDavTarget  # noqa: E402


DATE = "2026-09-03"
BASE = (*migration.CANONICAL, DATE)


class Response:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.headers = headers or {}
        self.body = io.BytesIO(body)
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.body.close()


class FakeDav:
    """In-memory HTTP WebDAV server. No provider or local file I/O."""

    def __init__(self):
        self.nodes = {migration.HOME: None}
        self.calls = []
        self.get_responses = []
        self.race_destination = None
        self.race_payload = None
        self.omit_etags = False
        self.change_after_get = None

    def add(self, path, content=None):
        for length in range(1, len(path)):
            self.nodes.setdefault(path[:length], None)
        self.nodes[path] = content

    def etag(self, path):
        if self.omit_etags:
            return ""
        payload = self.nodes[path]
        if payload is None:
            payload = repr(sorted(key for key in self.nodes if key[:-1] == path)).encode()
        return '"' + hashlib.sha256(payload).hexdigest() + '"'

    def path(self, url):
        return tuple(unquote(urlparse(url).path).removeprefix("/dav/").rstrip("/").split("/"))

    def error(self, request, status):
        raise HTTPError(request.full_url, status, "test error", {}, io.BytesIO())

    def xml(self, path, depth):
        root = ET.Element("{DAV:}multistatus")
        paths = [path] + sorted(item for item in self.nodes if depth == "1" and item[:-1] == path)
        for item in paths:
            content = self.nodes[item]
            response = ET.SubElement(root, "{DAV:}response")
            ET.SubElement(response, "{DAV:}href").text = "/dav/" + "/".join(quote(part, safe="") for part in item) + ("/" if content is None else "")
            propstat = ET.SubElement(response, "{DAV:}propstat")
            ET.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
            prop = ET.SubElement(propstat, "{DAV:}prop")
            resourcetype = ET.SubElement(prop, "{DAV:}resourcetype")
            if content is None:
                ET.SubElement(resourcetype, "{DAV:}collection")
            ET.SubElement(prop, "{DAV:}getcontentlength").text = str(len(content or b""))
            ET.SubElement(prop, "{DAV:}getetag").text = self.etag(item)
        return ET.tostring(root)

    def __call__(self, request, timeout):
        path = self.path(request.full_url)
        method = request.get_method()
        headers = {key.lower(): value for key, value in request.header_items()}
        self.calls.append((method, path, headers))
        if method == "PROPFIND":
            if path not in self.nodes:
                return self.error(request, 404)
            return Response(207, self.xml(path, headers["depth"]))
        if method == "MKCOL":
            if path in self.nodes:
                return self.error(request, 405)
            if path[:-1] and path[:-1] not in self.nodes:
                return self.error(request, 409)
            self.nodes[path] = None
            return Response(201)
        if path not in self.nodes:
            return self.error(request, 404)
        if method == "GET":
            response = Response(200, self.nodes[path], {"ETag": self.etag(path)})
            self.get_responses.append(response)
            if path == self.change_after_get:
                self.nodes[path] = b"changed-after-hash"
                self.change_after_get = None
            return response
        if method == "MOVE":
            destination = self.path(headers["destination"])
            assert headers.get("overwrite") == "F", "MOVE must disable overwrite"
            if destination == self.race_destination:
                self.nodes[destination] = self.race_payload
                self.race_destination = None
            if destination in self.nodes:
                return self.error(request, 412)
            assert destination[:-1] in self.nodes, "Destination folder must already exist"
            self.nodes[destination] = self.nodes.pop(path)
            return Response(201)
        if method == "DELETE":
            if headers.get("if-match") and headers["if-match"] != self.etag(path):
                return self.error(request, 412)
            if self.nodes[path] is None:
                assert not any(item[:len(path)] == path and item != path for item in self.nodes), "Must never delete a nonempty folder"
            else:
                assert path[-1] == ".DS_Store" or sum(call[0] == "GET" for call in self.calls) >= 2, "Video deletion requires both streamed hashes"
            del self.nodes[path]
            return Response(204)
        raise AssertionError(f"Unexpected HTTP method: {method}")

    def adapter(self, **kwargs):
        target = WebDavTarget("https://dav.jianguoyun.com/dav/", "test-user", "test-password",
                              migration.display(migration.CANONICAL), opener=self,
                              sleeper=lambda _: None)
        return migration.MigrationDav(target, **kwargs)


class ConsolidationTests(unittest.TestCase):
    def run_migration(self, server, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = migration.consolidate(server.adapter(), **kwargs)
        return result, output.getvalue()

    def test_audit_is_read_only_and_reports_source_and_canonical_counts(self):
        server = FakeDav()
        server.add((*migration.LEGACY_ROOTS[0], "Ops", DATE, "KC娱乐", "old.mp4"), b"old")
        server.add((*BASE, "Portal 娱乐", "current.mp4"), b"current")
        before = dict(server.nodes)
        result, output = self.run_migration(server)
        self.assertEqual(server.nodes, before)
        self.assertEqual({call[0] for call in server.calls}, {"PROPFIND"})
        self.assertEqual(result["moved"], 0)
        self.assertIn('"Portal 娱乐": 1', output)
        self.assertIn('"planned_files": 1', output)
        self.assertNotIn("test-password", output)
        self.assertNotIn("test-user", output)

    def test_mixed_roots_nested_files_and_category_normalization_are_idempotent(self):
        server = FakeDav()
        server.add((*migration.LEGACY_ROOTS[0], "Ops", DATE, "BBG Show", "show.mp4"), b"show")
        server.add((*migration.LEGACY_ROOTS[1], "Ops", DATE, "BBG Top Videos", "top.mp4"), b"top")
        server.add((*migration.LEGACY_ROOTS[2], DATE, "nested", "viral.mp4"), b"viral")
        server.add((*BASE, "KC娱乐", "ent.mp4"), b"ent")
        server.add((*BASE, "Portal 娱乐", "existing.mp4"), b"existing")
        server.add((*migration.HOME, "Unrelated", "keep.mp4"), b"unrelated")
        first, output = self.run_migration(server, apply=True)
        self.assertEqual(first["moved"], 4)
        self.assertEqual(server.nodes[(*BASE, "Portal 娱乐", "nested", "viral.mp4")], b"viral")
        self.assertEqual(server.nodes[(*BASE, "Portal 娱乐", "ent.mp4")], b"ent")
        self.assertEqual(server.nodes[(*migration.HOME, "Unrelated", "keep.mp4")], b"unrelated")
        self.assertFalse(any(root in server.nodes for root in migration.LEGACY_ROOTS))
        self.assertNotIn((*BASE, "KC娱乐"), server.nodes)
        self.assertIn('"Portal 娱乐": 3', output)
        before = dict(server.nodes)
        second, _ = self.run_migration(server, apply=True)
        self.assertEqual(second["moved"], 0)
        self.assertEqual(second["duplicates"], 0)
        self.assertEqual(before, server.nodes)
        self.assertTrue(all(call[1][:2] != (*migration.HOME, "Unrelated") for call in server.calls))

    def test_duplicate_requires_both_streamed_hashes_and_conditional_delete(self):
        server = FakeDav()
        source = (*migration.LEGACY_ROOTS[0], "Ops", DATE, "BBG Show", "duplicate.mp4")
        destination = (*BASE, "BBG Show", "duplicate.mp4")
        payload = b"same-video" * 150000
        server.add(source, payload)
        server.add(destination, payload)
        result, _ = self.run_migration(server, apply=True)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["moved"], 0)
        self.assertNotIn(source, server.nodes)
        self.assertEqual(server.nodes[destination], payload)
        self.assertEqual([call[1] for call in server.calls if call[0] == "GET"], [source, destination])
        deletion = next(call for call in server.calls if call[0] == "DELETE" and call[1] == source)
        self.assertIn("if-match", deletion[2])
        self.assertTrue(all(size == 1024 * 1024 for response in server.get_responses for size in response.read_sizes))

    def test_equal_size_different_content_preserves_both_with_stable_name(self):
        server = FakeDav()
        source = (*migration.LEGACY_ROOTS[2], DATE, "video.mp4")
        destination = (*BASE, "Portal 娱乐", "video.mp4")
        server.add(source, b"new")
        server.add(destination, b"old")
        result, _ = self.run_migration(server, apply=True)
        alternative = migration.collision_path(destination, source)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(server.nodes[destination], b"old")
        self.assertEqual(server.nodes[alternative], b"new")
        self.assertFalse(any(call[0] == "DELETE" and call[1] == source for call in server.calls))

    def test_missing_validators_preserves_duplicate_and_changed_destination_stops(self):
        source = (*migration.LEGACY_ROOTS[2], DATE, "same.mp4")
        destination = (*BASE, "Portal 娱乐", "same.mp4")
        server = FakeDav()
        server.omit_etags = True
        server.add(source, b"same")
        server.add(destination, b"same")
        result, _ = self.run_migration(server, apply=True)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(server.nodes[migration.collision_path(destination, source)], b"same")
        server = FakeDav()
        server.add(source, b"same")
        server.add(destination, b"same")
        server.change_after_get = destination
        with self.assertRaisesRegex(JianguoyunSyncError, "Destination changed"):
            self.run_migration(server, apply=True)
        self.assertEqual(server.nodes[source], b"same")
        self.assertEqual(server.nodes[destination], b"changed-after-hash")

    def test_duplicate_in_stable_collision_name_is_not_copied_again(self):
        server = FakeDav()
        source = (*migration.LEGACY_ROOTS[2], DATE, "video.mp4")
        destination = (*BASE, "Portal 娱乐", "video.mp4")
        alternative = migration.collision_path(destination, source)
        server.add(source, b"new")
        server.add(destination, b"old")
        server.add(alternative, b"new")
        result, _ = self.run_migration(server, apply=True)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(server.nodes[alternative], b"new")
        self.assertNotIn(source, server.nodes)

    def test_racing_destination_never_overwrites(self):
        server = FakeDav()
        source = (*migration.LEGACY_ROOTS[2], DATE, "video.mp4")
        destination = (*BASE, "Portal 娱乐", "video.mp4")
        server.add(source, b"new")
        server.race_destination, server.race_payload = destination, b"other"
        result, _ = self.run_migration(server, apply=True)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(server.nodes[destination], b"other")
        self.assertEqual(server.nodes[migration.collision_path(destination, source, 1)], b"new")

    def test_empty_cleanup_keeps_unmapped_data_and_unselected_dates(self):
        server = FakeDav()
        old = migration.LEGACY_ROOTS[0]
        server.add((*old, "Ops", DATE, "BBG Show", ".DS_Store"), b"metadata")
        server.add((*old, "Ops", DATE, "BBG Show", "today.mp4"), b"today")
        server.add((*old, "Ops", "2026-09-02", "BBG Show", "yesterday.mp4"), b"yesterday")
        server.add((*old, "Ops", DATE, "unknown.txt"), b"keep")
        server.add((*migration.LEGACY_ROOTS[1], ".DS_Store"), b"metadata")
        result, _ = self.run_migration(server, date_filter=DATE, apply=True)
        self.assertEqual(result["moved"], 1)
        self.assertEqual(result["metadata_removed"], 2)
        self.assertNotIn(migration.LEGACY_ROOTS[1], server.nodes)
        self.assertEqual(server.nodes[(*old, "Ops", DATE, "unknown.txt")], b"keep")
        self.assertEqual(server.nodes[(*old, "Ops", "2026-09-02", "BBG Show", "yesterday.mp4")], b"yesterday")
        self.assertIn(old, server.nodes)

    def test_ten_entertainment_videos_are_counted_after_migration(self):
        server = FakeDav()
        for number in range(5):
            server.add((*BASE, "KC娱乐", f"regular-{number}.mp4"), b"regular")
            server.add((*migration.LEGACY_ROOTS[2], DATE, f"viral-{number}.mp4"), b"viral")
        result, output = self.run_migration(server, apply=True)
        self.assertEqual(result["moved"], 10)
        self.assertIn('"Portal 娱乐": 10', output)

    def test_date_validation_and_scan_budget_fail_closed(self):
        server = FakeDav()
        with self.assertRaises(JianguoyunSyncError):
            self.run_migration(server, date_filter="2026-9-3", apply=True)
        with self.assertRaises(JianguoyunSyncError):
            with contextlib.redirect_stdout(io.StringIO()):
                migration.consolidate(server.adapter(max_entries=0), apply=True)
        self.assertFalse(any(call[0] in {"MOVE", "DELETE", "MKCOL"} for call in server.calls))


if __name__ == "__main__":
    unittest.main()

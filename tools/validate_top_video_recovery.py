#!/usr/bin/env python3
"""Validate a completed Top Videos run before reusing its cloud artifacts."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


WORKFLOW_PATH = ".github/workflows/daily-top-videos.yml"
ARTIFACT_PATTERN = re.compile(r"top-video-(\d{4}-\d{2}-\d{2})-([1-9][0-9]*)")


def validate_inputs(run_id: str, run_date: str) -> None:
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise ValueError("recover_run_id must be a positive numeric workflow run ID")
    try:
        parsed = date.fromisoformat(run_date)
    except ValueError as exc:
        raise ValueError("run_date must use strict YYYY-MM-DD format") from exc
    if parsed.isoformat() != run_date:
        raise ValueError("run_date must use strict YYYY-MM-DD format")


def validate_source(
    run: dict, artifacts: list[dict], repository: str, run_id: str, run_date: str
) -> list[int]:
    validate_inputs(run_id, run_date)
    if str(run.get("id")) != run_id:
        raise ValueError("Recovery source run ID does not match the requested run")
    for key in ("repository", "head_repository"):
        if str((run.get(key) or {}).get("full_name", "")).lower() != repository.lower():
            raise ValueError("Recovery source must belong to this repository")
    if run.get("path") != WORKFLOW_PATH or run.get("head_branch") != "main":
        raise ValueError("Recovery source must be daily-top-videos.yml on main")
    if run.get("event") not in {"schedule", "workflow_dispatch"}:
        raise ValueError("Recovery source must be a scheduled or manually dispatched run")
    if run.get("status") != "completed":
        raise ValueError("Recovery source must have completed before its artifacts are reused")

    manifests = [item for item in artifacts if item.get("name") == "top-videos-manifest"]
    if len(manifests) != 1 or manifests[0].get("expired", True):
        raise ValueError("Recovery source must have one unexpired top-videos-manifest artifact")

    indexes = []
    for item in artifacts:
        name = str(item.get("name", ""))
        if not name.startswith("top-video-"):
            continue
        match = ARTIFACT_PATTERN.fullmatch(name)
        if not match or match[1] != run_date:
            raise ValueError("Recovery video artifact dates must all match run_date")
        if item.get("expired", True):
            raise ValueError("Recovery video artifacts have expired")
        index = int(match[2])
        if index > 12 or index in indexes:
            raise ValueError("Recovery video artifact indexes must be unique and between 1 and 12")
        indexes.append(index)
    if not indexes:
        raise ValueError("Recovery source has no video artifacts for run_date")
    return sorted(indexes)


def api_json(path: str, token: str) -> dict:
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    request = Request(
        f"{api_url}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-date", required=True)
    args = parser.parse_args()
    try:
        validate_inputs(args.run_id, args.run_date)
        token = os.environ.get("GH_TOKEN", "")
        if not token:
            raise ValueError("GH_TOKEN with actions:read is required for artifact recovery")
        path = f"/repos/{args.repository}/actions/runs/{args.run_id}"
        run = api_json(path, token)
        artifacts = []
        page = 1
        while True:
            payload = api_json(f"{path}/artifacts?per_page=100&page={page}", token)
            batch = payload["artifacts"]
            artifacts.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        indexes = validate_source(run, artifacts, args.repository, args.run_id, args.run_date)
    except HTTPError as exc:
        print(f"Recovery source lookup failed: GitHub API returned HTTP {exc.code}", file=sys.stderr)
        return 1
    except URLError:
        print("Recovery source lookup failed: GitHub API connection failed", file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError) as exc:
        print(f"Invalid recovery source: {exc}", file=sys.stderr)
        return 1
    print(f"artifact_run_id={args.run_id}")
    print("recovery_mode=true")
    print("video_indexes=" + json.dumps(indexes, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

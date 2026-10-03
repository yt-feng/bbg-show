#!/usr/bin/env python3
"""Accept rendered clips or a complete, explicit sensitive-topic exclusion receipt."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def verify(manifest: dict, summary: dict) -> dict:
    inputs = manifest.get("videos")
    decisions = summary.get("videos")
    if not isinstance(inputs, list) or not isinstance(decisions, list) or not inputs:
        raise ValueError("Expected a nonempty source manifest and processing decisions")
    urls = [item.get("url") for item in inputs if isinstance(item, dict)]
    if len(urls) != len(inputs) or any(not isinstance(url, str) or not url.strip() for url in urls):
        raise ValueError("Invalid manifest source identity")
    if len(set(urls)) != len(urls):
        raise ValueError("Duplicate manifest source identity")
    if len(decisions) != len(inputs) or {d.get("url") for d in decisions} != set(urls):
        raise ValueError("Not every selected source has exactly one processing decision")
    counts = {"success": 0, "skipped": 0, "failed": 0}
    rendered = 0
    output = Path(summary["output_dir"]).resolve()
    for decision in decisions:
        status = decision.get("status")
        if status not in counts:
            raise ValueError("Unknown processing status")
        counts[status] += 1
        if status == "failed":
            raise ValueError("ARK processing failed; no empty-output exemption")
        if status == "skipped":
            if decision.get("reason") != "sensitive_topic":
                raise ValueError("Only explicit sensitive-topic exclusions allow no output")
            continue
        files = decision.get("rendered_files")
        if not isinstance(files, list) or not files:
            raise ValueError("Successful source did not declare rendered clips")
        for filename in files:
            path = (Path(decision["output_dir"]) / filename).resolve()
            if not path.is_relative_to(output) or path.suffix != ".mp4" or not path.is_file() or path.stat().st_size == 0:
                raise ValueError("Missing, empty, or out-of-scope rendered clip")
            rendered += 1
    for key, status in (("succeeded", "success"), ("skipped", "skipped"), ("failed", "failed")):
        if summary.get(key) != counts[status]:
            raise ValueError("Summary counters do not match processing decisions")
    if summary.get("total") != len(decisions):
        raise ValueError("Summary total does not match processing decisions")
    return {"selected": len(inputs), "rendered_clips": rendered, "skipped_sensitive_topic": counts["skipped"],
            "outcome": "rendered" if rendered else "all_selected_sources_excluded"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--step-summary", type=Path)
    args = parser.parse_args()
    receipt = verify(json.loads(args.manifest.read_text()), json.loads(args.summary.read_text()))
    text = json.dumps(receipt, sort_keys=True)
    print(text)
    if args.step_summary:
        with args.step_summary.open("a") as stream:
            stream.write("\n### ARK processing acceptance\n\n```json\n" + text + "\n```\n")


if __name__ == "__main__":
    main()

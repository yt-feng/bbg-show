#!/usr/bin/env python3
"""Read the structured outcome written by the title refiner."""

from __future__ import annotations

import json
from pathlib import Path


VALID_TITLE_REFINEMENT_STATUSES = frozenset({
    "refined",
    "partial_refined",
    "planner_fallback",
})


def read_title_refinement_status(plan_path: Path) -> str:
    """Return a validated ``plan.title_refine.status`` value."""
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    title_refine = plan.get("title_refine")
    if not isinstance(title_refine, dict):
        raise ValueError("refined plan has no title_refine object")
    status = str(title_refine.get("status", "")).strip()
    if status not in VALID_TITLE_REFINEMENT_STATUSES:
        raise ValueError(f"refined plan has invalid title_refine.status: {status!r}")
    return status

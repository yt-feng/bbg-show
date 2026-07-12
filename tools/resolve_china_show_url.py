#!/usr/bin/env python3
"""Resolve the Bloomberg show URL for a Beijing-date run."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from trump_filter import is_trump_related
from weekend_processed_shows import (
    WeekendProcessedShowsError,
    load_processed_shows,
    processed_show_dates,
)


SHOW_CHINA = "china"
SHOW_WEEKEND = "weekend"
SHOW_AUTO = "auto"
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEEKEND_BACKLOG = REPO_ROOT / "tools" / "bloomberg_weekend_backlog.json"
DEFAULT_RENDERED_ROOT = REPO_ROOT / "rendered-clips"
UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
PROBE_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/137.0.0.0 Safari/537.36"
)


def default_show_date() -> str:
    now_bj = datetime.now(ZoneInfo("Asia/Shanghai"))
    return (now_bj.date() - timedelta(days=1)).isoformat()


def resolve_show_type(show_date: str, show_type: str, url: str = "") -> str:
    requested = show_type.strip().lower() or SHOW_AUTO
    if requested not in {SHOW_AUTO, SHOW_CHINA, SHOW_WEEKEND}:
        raise SystemExit(f"Unknown show type: {show_type!r}. Expected auto, china, or weekend.")

    if requested != SHOW_AUTO:
        return requested

    lowered_url = url.lower()
    if "bloomberg-this-weekend" in lowered_url:
        return SHOW_WEEKEND
    if "the-china-show" in lowered_url:
        return SHOW_CHINA

    weekday = datetime.strptime(show_date, "%Y-%m-%d").date().weekday()
    return SHOW_WEEKEND if weekday >= 5 else SHOW_CHINA


def build_china_url(show_date: str) -> str:
    year_s, month_s, day_s = show_date.split("-")
    year = int(year_s)
    month = int(month_s)
    day = int(day_s)
    return (
        f"https://www.bloomberg.com/news/videos/{show_date}/"
        f"the-china-show-{month}-{day}-{year}-video"
    )


def build_weekend_url(show_date: str) -> str:
    year_s, month_s, day_s = show_date.split("-")
    year = int(year_s)
    month = int(month_s)
    return (
        f"https://www.bloomberg.com/news/videos/{show_date}/"
        f"bloomberg-this-weekend-{month}-{day_s}-{year}-video"
    )


def build_url(show_date: str, show_type: str) -> str:
    if show_type == SHOW_WEEKEND:
        return build_weekend_url(show_date)
    return build_china_url(show_date)


def bloomberg_brp_url(url: str) -> str:
    parsed = urlsplit(url)
    return parsed._replace(scheme="https", netloc="brp-prod-bcc.bloomberg.com").geturl()


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def load_weekend_backlog(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []

    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw_items = raw.get("videos", [])
    elif isinstance(raw, list):
        raw_items = raw
    else:
        raise SystemExit(f"Unsupported weekend backlog format in {path}")

    items: list[dict[str, str]] = []
    for entry in raw_items:
        if isinstance(entry, str):
            item = {"date": entry}
        elif isinstance(entry, dict):
            item = {str(key): str(value) for key, value in entry.items() if value is not None}
        else:
            raise SystemExit(f"Unsupported weekend backlog entry in {path}: {entry!r}")

        date_value = item.get("date", "").strip()
        if not date_value:
            raise SystemExit(f"Weekend backlog entry missing date in {path}: {entry!r}")
        parse_date(date_value)
        item["date"] = date_value
        items.append(item)

    items.sort(key=lambda item: item["date"])
    return items


def has_rendered_mp4(rendered_root: Path, show_date: str) -> bool:
    output_dir = rendered_root / show_date
    if not output_dir.is_dir():
        return False
    return any(output_dir.rglob("*.mp4"))


def probe_weekend_url(show_date: str, timeout: int) -> tuple[bool, str]:
    url = build_weekend_url(show_date)
    brp_url = bloomberg_brp_url(url)
    curl_cmd = [
        "curl",
        "-sS",
        "--fail",
        "--location",
        "--max-time",
        str(max(3, timeout)),
        "--connect-timeout",
        str(max(2, min(timeout, 5))),
        "-A",
        PROBE_UA,
        "-H",
        "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        brp_url,
    ]
    curl_error = ""
    try:
        proc = subprocess.run(
            curl_cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=max(8, timeout + 5),
        )
        if proc.returncode == 0:
            return classify_weekend_probe_content(proc.stdout)
        curl_error = (proc.stderr or proc.stdout or f"curl exited {proc.returncode}").strip()
        if "404" in curl_error:
            return False, "HTTP 404"
        return False, curl_error or f"curl exited {proc.returncode}"
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)


def classify_weekend_probe_content(content: str, status: int = 200) -> tuple[bool, str]:
    if status >= 400:
        return False, f"HTTP {status}"
    if "bloomberg-this-weekend" not in content.lower():
        return False, "not a Bloomberg Weekend page"
    if not UUID_RE.search(content) and "media-manifest" not in content and ".m3u8" not in content:
        return False, "no video asset marker"
    return True, "available"


def iter_older_weekend_items(start_date: date, days: int, seen_dates: set[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    current = start_date
    earliest = start_date - timedelta(days=max(0, days))
    while current >= earliest:
        show_date = current.isoformat()
        if current.weekday() >= 5 and show_date not in seen_dates:
            items.append(
                {
                    "date": show_date,
                    "title": f"Bloomberg This Weekend {current.month}/{current.day:02d}/{current.year}",
                    "source": "weekend-history",
                }
            )
        current -= timedelta(days=1)
    return items


def weekend_candidate_items(
    backlog_path: Path,
    cutoff_date: str,
    history_days: int,
    excluded_dates: set[str] | None = None,
) -> list[dict[str, str]]:
    cutoff = parse_date(cutoff_date)
    excluded_dates = excluded_dates or set()
    backlog = [
        item
        for item in load_weekend_backlog(backlog_path)
        if (
            parse_date(item["date"]) <= cutoff
            and item["date"] not in excluded_dates
            and not is_trump_related(item)
        )
    ]
    for item in backlog:
        item.setdefault("source", "weekend-backlog")

    seen_dates = {item["date"] for item in backlog} | excluded_dates
    if backlog:
        oldest = min(parse_date(item["date"]) for item in backlog)
        history_start = oldest - timedelta(days=1)
    else:
        history_start = cutoff

    return backlog + iter_older_weekend_items(history_start, history_days, seen_dates)


def choose_weekend_backlog_item(
    backlog_path: Path,
    rendered_root: Path,
    cutoff_date: str,
    history_days: int,
    probe_timeout: int,
    probe_availability: bool,
    max_history_probes: int,
    processed_dates: set[str] | None = None,
) -> dict[str, str] | None:
    processed_dates = processed_dates or set()
    current_date = parse_date(cutoff_date)
    current_show_date = current_date.isoformat()

    if current_show_date in processed_dates:
        print(f"Skipping Weekend current candidate {current_show_date}: terminal state already recorded", flush=True)
    elif has_rendered_mp4(rendered_root, current_show_date):
        print(f"Skipping Weekend current candidate {current_show_date}: rendered MP4 already exists", flush=True)
    else:
        current_item = {
            "date": current_show_date,
            "title": f"Bloomberg This Weekend {current_date.month}/{current_date.day:02d}/{current_date.year}",
            "source": "weekend-current",
        }
        if not probe_availability:
            return current_item
        available, reason = probe_weekend_url(current_show_date, probe_timeout)
        print(f"Weekend current candidate {current_show_date}: {reason}", flush=True)
        if available:
            return current_item

    history_probes = 0
    for item in weekend_candidate_items(
        backlog_path,
        cutoff_date,
        history_days,
        excluded_dates={current_show_date},
    ):
        if item["date"] in processed_dates:
            print(f"Skipping Weekend candidate {item['date']}: terminal state already recorded", flush=True)
            continue
        if has_rendered_mp4(rendered_root, item["date"]):
            print(f"Skipping Weekend candidate {item['date']}: rendered MP4 already exists", flush=True)
            continue
        should_probe = probe_availability and item.get("source") == "weekend-history"
        if should_probe:
            if history_probes >= max_history_probes:
                print(f"Weekend history probe cap reached ({max_history_probes}); stopping scan.", flush=True)
                break
            history_probes += 1
            available, reason = probe_weekend_url(item["date"], probe_timeout)
            print(f"Weekend candidate {item['date']}: {reason}", flush=True)
            if not available:
                continue
        return item
    return None


def append_env(path: Path, values: dict[str, str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-date", default="", help="YYYY-MM-DD. Defaults to yesterday in Asia/Shanghai.")
    parser.add_argument(
        "--show-type",
        choices=[SHOW_AUTO, SHOW_CHINA, SHOW_WEEKEND],
        default=SHOW_AUTO,
        help="auto chooses China Show on Mon-Fri show dates and Bloomberg Weekend on Sat-Sun show dates.",
    )
    parser.add_argument("--url", default="", help="Override Bloomberg video URL.")
    parser.add_argument(
        "--weekend-backlog",
        type=Path,
        default=DEFAULT_WEEKEND_BACKLOG,
        help="JSON list of Bloomberg Weekend dates to drain from oldest unrendered date.",
    )
    parser.add_argument(
        "--rendered-root",
        type=Path,
        default=DEFAULT_RENDERED_ROOT,
        help="Rendered clips root used to skip already processed backlog dates.",
    )
    parser.add_argument(
        "--weekend-processed-shows",
        type=Path,
        default=None,
        help=(
            "Strict terminal-state ledger for Weekend shows. "
            "Defaults to <rendered-root>/weekend/processed_shows.json."
        ),
    )
    parser.add_argument(
        "--weekend-history-days",
        type=int,
        default=730,
        help="After configured backlog dates are done, scan this many older days for Weekend shows.",
    )
    parser.add_argument(
        "--weekend-probe-timeout",
        type=int,
        default=8,
        help="Seconds per quick BRP availability probe for generated older Weekend candidates.",
    )
    parser.add_argument(
        "--weekend-max-history-probes",
        type=int,
        default=24,
        help="Maximum generated older Weekend dates to probe after configured backlog dates are done.",
    )
    parser.add_argument(
        "--no-weekend-availability-probe",
        action="store_true",
        help="Disable quick BRP availability probes for auto Weekend selection.",
    )
    parser.add_argument("--github-env", type=Path, default=None)
    parser.add_argument("--metadata", type=Path, default=None)
    args = parser.parse_args()

    explicit_show_date = bool(args.show_date.strip())
    explicit_url = bool(args.url.strip())
    fallback_show_date = default_show_date()
    show_date = args.show_date.strip() or fallback_show_date
    show_type = resolve_show_type(show_date, args.show_type, args.url)
    resolution_source = "explicit" if explicit_show_date or explicit_url else "default"
    backlog_title = ""
    backlog_duration = ""
    skip_show = False
    skip_reason = ""

    if show_type == SHOW_WEEKEND and not explicit_show_date and not explicit_url:
        processed_shows_path = (
            args.weekend_processed_shows
            if args.weekend_processed_shows is not None
            else args.rendered_root / "weekend" / "processed_shows.json"
        )
        try:
            weekend_ledger = load_processed_shows(processed_shows_path)
        except WeekendProcessedShowsError as exc:
            raise SystemExit(str(exc)) from exc
        backlog_item = choose_weekend_backlog_item(
            args.weekend_backlog,
            args.rendered_root,
            cutoff_date=fallback_show_date,
            history_days=args.weekend_history_days,
            probe_timeout=args.weekend_probe_timeout,
            probe_availability=not args.no_weekend_availability_probe,
            max_history_probes=args.weekend_max_history_probes,
            processed_dates=processed_show_dates(weekend_ledger),
        )
        if backlog_item:
            show_date = backlog_item["date"]
            resolution_source = backlog_item.get("source", "weekend-backlog")
            backlog_title = backlog_item.get("title", "")
            backlog_duration = backlog_item.get("duration", "")
        else:
            skip_show = True
            skip_reason = (
                "No unprocessed available Bloomberg Weekend show was found in the current date, "
                "configured backlog, or older weekend scan."
            )

    url = args.url.strip() or build_url(show_date, show_type)
    output_dir = f"rendered-clips/{show_date}"

    values = {
        "SHOW_DATE": show_date,
        "SHOW_TYPE": show_type,
        "SHOW_URL": url,
        "OUTPUT_DIR": output_dir,
        "SHOW_RESOLUTION_SOURCE": resolution_source,
        "SKIP_SHOW": "true" if skip_show else "false",
    }
    if backlog_title:
        values["SHOW_BACKLOG_TITLE"] = backlog_title
    if backlog_duration:
        values["SHOW_BACKLOG_DURATION"] = backlog_duration
    if skip_reason:
        values["SHOW_SKIP_REASON"] = skip_reason

    print(f"SHOW_DATE={show_date}", flush=True)
    print(f"SHOW_TYPE={show_type}", flush=True)
    print(f"SHOW_URL={url}", flush=True)
    print(f"OUTPUT_DIR={output_dir}", flush=True)
    print(f"SHOW_RESOLUTION_SOURCE={resolution_source}", flush=True)
    print(f"SKIP_SHOW={'true' if skip_show else 'false'}", flush=True)
    if backlog_title:
        print(f"SHOW_BACKLOG_TITLE={backlog_title}", flush=True)
    if backlog_duration:
        print(f"SHOW_BACKLOG_DURATION={backlog_duration}", flush=True)
    if skip_reason:
        print(f"SHOW_SKIP_REASON={skip_reason}", flush=True)

    if args.github_env:
        append_env(args.github_env, values)
    elif os.environ.get("GITHUB_ENV"):
        append_env(Path(os.environ["GITHUB_ENV"]), values)

    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

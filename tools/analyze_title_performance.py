#!/usr/bin/env python3
"""Analyze WeChat Channels title performance CSV exports."""

from __future__ import annotations

import argparse
import csv
import re
import statistics
from pathlib import Path
from typing import Callable


def parse_int(value: str) -> int:
    value = str(value or "").replace(",", "").strip()
    return int(float(value)) if value else 0


def parse_float(value: str) -> float:
    value = str(value or "").replace("%", "").replace("秒", "").strip()
    return float(value) if value else 0.0


def title_from_desc(desc: str) -> str:
    return str(desc or "").split("#", 1)[0].strip(" ，,。")


def load_rows(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for raw in csv.DictReader(fh):
            title = title_from_desc(str(raw.get("视频描述", "")))
            rows.append({
                "title": title,
                "views": parse_int(str(raw.get("播放量", ""))),
                "completion": parse_float(str(raw.get("完播率", ""))),
                "avg_sec": parse_float(str(raw.get("平均播放时长", ""))),
                "likes": parse_int(str(raw.get("喜欢", ""))),
                "shares": parse_int(str(raw.get("分享量", ""))),
                "follows": parse_int(str(raw.get("关注量", ""))),
            })
    return rows


def engagement_score(row: dict[str, object]) -> float:
    views = float(row["views"])
    return (
        views * (1 + float(row["completion"]) / 100)
        + 20 * float(row["likes"])
        + 30 * float(row["shares"])
        + 50 * float(row["follows"])
    )


def pattern_summary(rows: list[dict[str, object]], name: str, fn: Callable[[str], bool]) -> str:
    yes = [row for row in rows if fn(str(row["title"]))]
    no = [row for row in rows if not fn(str(row["title"]))]
    if not yes or not no:
        return ""
    return (
        f"{name:16s} n={len(yes):3d} "
        f"views={statistics.mean(float(row['views']) for row in yes):8.1f} vs "
        f"{statistics.mean(float(row['views']) for row in no):8.1f}; "
        f"completion={statistics.mean(float(row['completion']) for row in yes):5.2f} vs "
        f"{statistics.mean(float(row['completion']) for row in no):5.2f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    rows = load_rows(args.csv_path)
    if not rows:
        raise SystemExit("No rows found")

    by_score = sorted(rows, key=engagement_score, reverse=True)
    print(f"Rows: {len(rows)}")
    print("\nTop titles")
    for row in by_score[: args.top]:
        print(
            f"{engagement_score(row):9.1f} score "
            f"{int(row['views']):6d} views {float(row['completion']):5.2f}% "
            f"| {row['title']}"
        )

    patterns: dict[str, Callable[[str], bool]] = {
        "question": lambda t: bool(re.search(r"[?？]|吗|为何|为什么|怎么|能否|是否", t)),
        "big_anchor": lambda t: bool(re.search(r"高盛|摩根|美联储|马斯克|黄仁勋|辜朝明|洪灏|中金|野村|美银|腾讯|阿里|英伟达|ARK|木头姐", t, re.I)),
        "number_data": lambda t: bool(re.search(r"\d|万亿|万|亿|%|美元|基点|PMI|CPI|PCE", t, re.I)),
        "china_related": lambda t: bool(re.search(r"中国|A股|港股|人民币|楼市|房价|房地产|地产|消费|内需", t)),
        "hook_words": lambda t: bool(re.search(r"关键|信号|反转|真相|意外|被逼|陷阱|托底|转向|拐点|分歧|重估|掩盖|冰火两重天", t)),
        "long_gt_24": lambda t: len(t) > 24,
    }
    print("\nPattern averages")
    for name, fn in patterns.items():
        line = pattern_summary(rows, name, fn)
        if line:
            print(line)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Group video files by similar timestamps (within 1 minute)."""

import re
from pathlib import Path
from datetime import datetime

VIDEO_EXTENSIONS = {".mp4", ".avi", ".mkv", ".mov", ".ts", ".flv", ".wmv"}
PATTERN = re.compile(r"recording_(\d{8})_(\d{6})_idx_(\d+)")
ROOT = Path(__file__).parent


def parse_timestamp(filename: str) -> datetime | None:
    m = PATTERN.match(filename)
    if not m:
        return None
    date_str, time_str, _ = m.groups()
    return datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")


def find_videos(root: Path) -> list[tuple[Path, datetime]]:
    videos = []
    for f in root.rglob("*"):
        if f.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        if f.name.startswith("._"):
            continue
        ts = parse_timestamp(f.name)
        if ts is None:
            continue
        videos.append((f, ts))
    videos.sort(key=lambda x: x[1])
    return videos


def group_similar(
    videos: list[tuple[Path, datetime]], threshold_sec: int = 60
) -> list[list[tuple[Path, datetime]]]:
    if not videos:
        return []
    groups = [[videos[0]]]
    for path, ts in videos[1:]:
        if (ts - groups[-1][-1][1]).total_seconds() <= threshold_sec:
            groups[-1].append((path, ts))
        else:
            groups.append([(path, ts)])
    return groups


def main():
    videos = find_videos(ROOT)
    if not videos:
        print("No video files found.")
        return

    groups = group_similar(videos)

    print(f"Found {len(videos)} videos in {len(groups)} time group(s).\n")
    for i, group in enumerate(groups, 1):
        if len(group) < 2:
            continue
        base_ts = group[0][1]
        print(f"--- Group {i} (around {base_ts.strftime('%Y-%m-%d %H:%M:%S')}) ---")
        for path, ts in group:
            delta = (ts - base_ts).total_seconds()
            print(f"  {path}  (+{delta:.0f}s)")
        print()


if __name__ == "__main__":
    main()

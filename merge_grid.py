#!/usr/bin/env python3
"""Merge 6 camera videos into a 2x3 grid using ffmpeg, with time sync.

Layout (2 rows x 3 columns):
  Row 1: robot_front(1)    launcher_front(2)   rotor(3)
  Row 2: stator(4)         launcher_L(5)       launcher_R(6)

Videos are synced by their filename timestamps: cameras that started
recording later are delayed with black frames so all frames represent
the same wall-clock time. Missing cameras get full black frames.
Shorter videos are padded with black at the end.

Usage:
  uv run merge_grid.py -g 0 -o output.mp4            # merge time group 0
  uv run merge_grid.py -g 0 -o out.mp4 --duration 10 # only first 10s (test)
  uv run merge_grid.py v1.mp4 v2.mp4 ... -o out.mp4  # explicit files (camera order)
"""

import argparse
import subprocess
import sys
from pathlib import Path
from datetime import datetime

from group_similar_videos import parse_timestamp

CAMERA_ORDER = [
    "camera_robot_front",
    "camera_launcher_front",
    "camera_rotor",
    "camera_stator",
    "camera_launcher_L",
    "camera_launcher_R",
]

COLS = 3
ROWS = 2
WIDTH = 1920
HEIGHT = 1080
FPS = 30


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        sys.exit(f"ffprobe failed for {path}")
    return float(result.stdout.strip())


def find_group_videos(root: Path, group_index: int) -> dict[str, tuple[Path, datetime]]:
    from group_similar_videos import find_videos, group_similar

    videos = find_videos(root)
    groups = group_similar(videos)
    if group_index >= len(groups):
        sys.exit(f"Group {group_index} not found (only {len(groups)} groups)")

    slots = {}
    for path, ts in groups[group_index]:
        for cam in CAMERA_ORDER:
            if cam in str(path):
                slots[cam] = (path, ts)
    return slots


def merge_grid(
    slots: dict[str, tuple[Path, datetime] | None],
    output: Path,
    limit: float | None,
):
    n = len(CAMERA_ORDER)

    timestamps = {}
    durations = {}
    for cam in CAMERA_ORDER:
        entry = slots.get(cam)
        if entry is not None:
            path, ts = entry
            timestamps[cam] = ts
            durations[cam] = probe_duration(path)

    if not durations:
        sys.exit("No valid input videos.")

    ref_ts = min(timestamps.values())
    delays = {cam: (timestamps[cam] - ref_ts).total_seconds() for cam in timestamps}
    ends = {cam: delays[cam] + durations[cam] for cam in durations}
    total_duration = max(ends.values())

    print(f"  {'camera':25s} {'delay':>7s} {'duration':>9s} {'end':>7s}")
    for cam in CAMERA_ORDER:
        entry = slots.get(cam)
        if entry is None:
            print(f"  {cam:25s} {'--':>7s} {'--':>9s} {'--':>7s}  MISSING")
        else:
            print(
                f"  {cam:25s} {delays[cam]:6.1f}s {durations[cam]:8.1f}s"
                f" {ends[cam]:6.1f}s  {entry[0].name}"
            )
    print(f"  total duration: {total_duration:.1f}s")

    cmd = ["ffmpeg", "-y"]
    filter_parts = []
    labels = []

    for i, cam in enumerate(CAMERA_ORDER):
        entry = slots.get(cam)
        if entry is None:
            cmd += [
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d={total_duration:.3f}",
            ]
        else:
            cmd += ["-i", str(entry[0])]

        delay = delays.get(cam, 0.0)
        tail_pad = max(0.0, total_duration - ends.get(cam, 0.0))

        parts = (
            f"[{i}:v]fps={FPS},"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
        )
        if delay > 0:
            parts += f",tpad=start_duration={delay:.3f}:start_mode=add:color=black"
        if tail_pad > 0:
            parts += f",tpad=stop_duration={tail_pad:.3f}:stop_mode=add:color=black"
        parts += f"[v{i}]"

        filter_parts.append(parts)
        labels.append(f"[v{i}]")

    layout = "|".join(f"{(i % COLS) * WIDTH}_{(i // COLS) * HEIGHT}" for i in range(n))
    filter_parts.append(f"{''.join(labels)}xstack=inputs={n}:layout={layout}[out]")

    effective_duration = limit if limit is not None else total_duration
    cmd += [
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[out]",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "23",
        "-r",
        str(FPS),
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        "-t",
        f"{effective_duration:.3f}",
    ]
    cmd.append(str(output))

    print(
        f"Output: {output} ({WIDTH * COLS}x{HEIGHT * ROWS}, {effective_duration:.1f}s)"
    )
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit("ffmpeg failed")
    print("Done.")


def main():
    parser = argparse.ArgumentParser(description="Merge 6 camera videos into 2x3 grid.")
    parser.add_argument(
        "inputs", nargs="*", type=Path, help="Input videos in camera order (6 files)"
    )
    parser.add_argument("-o", "--output", type=Path, default=Path("grid_output.mp4"))
    parser.add_argument(
        "-g", "--group", type=int, default=None, help="Time group index (0-based)"
    )
    parser.add_argument(
        "--duration", type=float, default=None, help="Limit output duration (seconds)"
    )
    args = parser.parse_args()

    root = Path(__file__).parent

    if args.group is not None:
        found = find_group_videos(root, args.group)
        slots: dict[str, tuple[Path, datetime] | None] = {
            cam: found.get(cam) for cam in CAMERA_ORDER
        }
    elif len(args.inputs) == len(CAMERA_ORDER):
        slots = {}
        for cam, path in zip(CAMERA_ORDER, args.inputs):
            ts = parse_timestamp(path.name)
            if ts is None:
                parser.error(f"Cannot parse timestamp from {path.name}")
            slots[cam] = (path, ts)
    else:
        parser.error(
            f"Provide exactly {len(CAMERA_ORDER)} input files, or use -g GROUP"
        )

    merge_grid(slots, args.output, args.duration)


if __name__ == "__main__":
    main()

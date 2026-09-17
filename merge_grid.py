#!/usr/bin/env python3
"""Merge 6 camera videos into a 2x3 grid using ffmpeg.

Layout (2 rows x 3 columns):
  Row 1: robot_front(1)    launcher_front(2)   rotor(3)
  Row 2: stator(4)         launcher_L(5)       launcher_R(6)

Missing cameras are replaced with black frames. Shorter videos are
padded with black frames to match the longest video's duration.

Usage:
  uv run merge_grid.py -g 0 -o output.mp4            # merge time group 0
  uv run merge_grid.py -g 0 -o out.mp4 --duration 10 # only first 10s (test)
  uv run merge_grid.py v1.mp4 v2.mp4 ... -o out.mp4  # explicit files (camera order)
"""

import argparse
import subprocess
import sys
from pathlib import Path

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


def find_group_videos(root: Path, group_index: int) -> dict[str, Path]:
    from group_similar_videos import find_videos, group_similar

    videos = find_videos(root)
    groups = group_similar(videos)
    if group_index >= len(groups):
        sys.exit(f"Group {group_index} not found (only {len(groups)} groups)")

    slots = {}
    for path, _ in groups[group_index]:
        for cam in CAMERA_ORDER:
            if cam in str(path):
                slots[cam] = path
    return slots


def merge_grid(
    slots: dict[str, Path | None],
    durations: dict[str, float],
    output: Path,
    max_duration: float,
    limit: float | None,
):
    n = len(CAMERA_ORDER)
    cmd = ["ffmpeg", "-y"]
    filter_parts = []
    labels = []

    for i, cam in enumerate(CAMERA_ORDER):
        path = slots.get(cam)
        if path is None:
            cmd += [
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}:d={max_duration:.3f}",
            ]
            pad_sec = 0.0
        else:
            cmd += ["-i", str(path)]
            pad_sec = max(0.0, max_duration - durations[cam])

        filter_parts.append(
            f"[{i}:v]fps={FPS},"
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
            f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black"
            + (
                f",tpad=stop=-1:stop_mode=add:color=black:stop_duration={pad_sec:.3f}"
                if pad_sec > 0
                else ""
            )
            + f"[v{i}]"
        )
        labels.append(f"[v{i}]")

    layout = "|".join(f"{(i % COLS) * WIDTH}_{(i // COLS) * HEIGHT}" for i in range(n))
    filter_parts.append(f"{''.join(labels)}xstack=inputs={n}:layout={layout}[out]")

    effective_duration = limit if limit is not None else max_duration
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
        f"Output: {output} ({WIDTH * COLS}x{HEIGHT * ROWS}, duration {max_duration:.1f}s)"
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
        slots = {cam: found.get(cam) for cam in CAMERA_ORDER}
    elif len(args.inputs) == len(CAMERA_ORDER):
        slots = dict(zip(CAMERA_ORDER, args.inputs))
    else:
        parser.error(
            f"Provide exactly {len(CAMERA_ORDER)} input files, or use -g GROUP"
        )

    durations = {}
    for cam in CAMERA_ORDER:
        path = slots[cam]
        if path is None:
            print(f"  {cam:25s} MISSING -> black frame")
        else:
            durations[cam] = probe_duration(path)
            print(f"  {cam:25s} {path.name}  ({durations[cam]:.1f}s)")

    max_duration = max(durations.values()) if durations else 0.0
    if max_duration <= 0:
        sys.exit("No valid input videos.")

    for cam in CAMERA_ORDER:
        if cam in durations and durations[cam] < max_duration:
            print(f"  pad {cam}: +{max_duration - durations[cam]:.1f}s black")

    merge_grid(slots, durations, args.output, max_duration, args.duration)


if __name__ == "__main__":
    main()

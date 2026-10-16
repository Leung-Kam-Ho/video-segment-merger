#!/usr/bin/env python3
"""Build synchronized 2x3 grid videos, one per recording session.

All six camera folders write segments whose filenames contain the recording
start time (``recording_YYYYMMDD_HHMMSS_idx_N.mp4``).  This script clusters the
day's videos into sessions (videos whose timestamps are close together),
picks the **earliest** timestamp of each session as the reference, and aligns
every camera on that wall-clock time: cameras that started later get black
frames prepended, cameras that stopped earlier get black frames appended, and
missing cameras are replaced by full black.

The two front cameras recorded sped-up footage (frame drops); their real-time
versions produced by ``extent.py`` (``<camera>_5min/``) are used automatically
when available, falling back to the original file with a warning.

Layout (2 rows x 3 columns, robot_front and launcher_front in the center):

    launcher_L  | robot_front   | launcher_R
    rotor       | launcher_front| stator

Usage:
    uv run sync_grid.py --list                    # show detected sessions
    uv run sync_grid.py                           # render every session
    uv run sync_grid.py -g 0                      # render session 0 only
    uv run sync_grid.py -g 0 --duration 10 -o test.mp4   # quick 10 s test
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v"}
PATTERN = re.compile(r"recording_(\d{8})_(\d{6})_idx_(\d+)")

# Grid layout, row-major. Center column = the two front cameras.
CAMERA_ORDER = [
    "camera_launcher_L", "camera_robot_front", "camera_launcher_R",
    "camera_rotor", "camera_launcher_front", "camera_stator",
]
COLS = 3
ROWS = 2
CELL_W = 1920
CELL_H = 1080
FPS = 30


def parse_timestamp(filename: str) -> datetime | None:
    m = PATTERN.match(filename)
    if not m:
        return None
    date_str, time_str, _ = m.groups()
    return datetime.strptime(f"{date_str}_{time_str}", "%Y%m%d_%H%M%S")


def probe_duration(path: Path) -> float | None:
    """Duration in seconds, or None if the file is unreadable/corrupt."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return None
        duration = float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError):
        return None
    return duration if duration > 0 else None


def find_videos(root: Path) -> dict[str, list[tuple[Path, datetime]]]:
    """Timestamp-sorted videos per camera folder (extended copies preferred)."""
    per_camera: dict[str, list[tuple[Path, datetime]]] = {}
    for cam in CAMERA_ORDER:
        folder = root / cam
        entries: list[tuple[Path, datetime]] = []
        if folder.is_dir():
            for f in sorted(folder.iterdir()):
                if f.suffix.lower() not in VIDEO_EXTENSIONS or f.name.startswith("._"):
                    continue
                ts = parse_timestamp(f.name)
                if ts is None:
                    continue
                entries.append((f, ts))
        per_camera[cam] = sorted(entries, key=lambda e: e[1])
    return per_camera


def group_sessions(
    per_camera: dict[str, list[tuple[Path, datetime]]], threshold_sec: float
) -> list[dict[str, tuple[Path, datetime]]]:
    """Cluster videos across cameras into sessions by start timestamp."""
    everything = sorted(
        ((cam, path, ts) for cam, vids in per_camera.items() for path, ts in vids),
        key=lambda e: e[2],
    )
    sessions: list[dict[str, tuple[Path, datetime]]] = []
    for cam, path, ts in everything:
        if sessions and (ts - sessions[-1]["__ref__"][1]).total_seconds() <= threshold_sec:
            sessions[-1][cam] = (path, ts)
        else:
            sessions.append({cam: (path, ts), "__ref__": (path, ts)})
    return sessions


def render_grid(
    slots: dict[str, tuple[Path, datetime]],
    output: Path,
    limit: float | None,
    no_sync: bool,
    scale: float,
    crf: int,
    preset: str,
) -> None:
    cell_w, cell_h = int(CELL_W * scale) // 2 * 2, int(CELL_H * scale) // 2 * 2

    durations: dict[str, float] = {}
    for cam in CAMERA_ORDER:
        entry = slots.get(cam)
        if entry is None:
            continue
        path = entry[0]
        # Prefer the real-time version produced by extent.py, if present.
        extended = path.parent.parent / f"{path.parent.name}_5min" / path.name
        source = path
        if extended.exists():
            dur = probe_duration(extended)
            if dur:
                source, durations[cam] = extended, dur
                continue
        dur = probe_duration(path)
        if dur:
            durations[cam] = dur
            if path.parent.name in ("camera_launcher_front", "camera_robot_front"):
                print(f"  WARNING: no usable extended copy for {path.name}; "
                      f"using sped-up original (timeline will be off)")
        else:
            print(f"  WARNING: skipping unreadable file {path.name}")
        slots[cam] = (source, entry[1])

    if not durations:
        print("  nothing renderable in this session")
        return

    if no_sync:
        delays = {cam: 0.0 for cam in durations}
    else:
        ref_ts = min(slots[cam][1] for cam in durations)
        delays = {cam: (slots[cam][1] - ref_ts).total_seconds() for cam in durations}

    total_duration = max(delays[cam] + durations[cam] for cam in durations)

    if no_sync:
        print("  sync disabled (all videos start at t=0)")
    else:
        print(f"  reference (earliest): {ref_ts:%Y-%m-%d %H:%M:%S}")
    for cam in CAMERA_ORDER:
        if cam in durations:
            print(f"    {cam:22s} +{delays[cam]:5.1f}s  {durations[cam]:7.1f}s  {slots[cam][0]}")
        else:
            print(f"    {cam:22s}   --            MISSING")
    print(f"  total duration: {total_duration:.1f}s  grid {cell_w * COLS}x{cell_h * ROWS}")

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
    filter_parts = []
    labels = []

    for i, cam in enumerate(CAMERA_ORDER):
        if cam in durations:
            cmd += ["-i", str(slots[cam][0])]
        else:
            cmd += ["-f", "lavfi", "-i",
                    f"color=c=black:s={cell_w}x{cell_h}:r={FPS}:d={total_duration:.3f}"]

        parts = (f"[{i}:v]fps={FPS},"
                 f"scale={cell_w}:{cell_h}:force_original_aspect_ratio=decrease,"
                 f"pad={cell_w}:{cell_h}:(ow-iw)/2:(oh-ih)/2:black")
        delay = delays.get(cam, 0.0)
        if delay > 0:
            parts += f",tpad=start_duration={delay:.3f}:start_mode=add:color=black"
        tail_pad = max(0.0, total_duration - delay - durations.get(cam, total_duration))
        if tail_pad > 0:
            parts += f",tpad=stop_duration={tail_pad:.3f}:stop_mode=add:color=black"
        parts += f"[v{i}]"
        filter_parts.append(parts)
        labels.append(f"[v{i}]")

    layout = "|".join(f"{(i % COLS) * cell_w}_{(i // COLS) * cell_h}" for i in range(len(CAMERA_ORDER)))
    filter_parts.append(f"{''.join(labels)}xstack=inputs={len(CAMERA_ORDER)}:layout={layout}[out]")

    effective_duration = limit if limit is not None else total_duration
    cmd += ["-filter_complex", ";".join(filter_parts), "-map", "[out]",
            "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-movflags", "+faststart",
            "-t", f"{effective_duration:.3f}", str(output)]

    output.parent.mkdir(parents=True, exist_ok=True)
    print(f"  -> {output}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        sys.exit(f"ffmpeg failed for {output.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build timestamp-aligned 2x3 grid videos, one per session.")
    parser.add_argument("-g", "--group", type=int, default=None,
                        help="render only this session index (see --list)")
    parser.add_argument("--list", action="store_true", help="list detected sessions and exit")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("grids"),
                        help="output folder (default: grids/)")
    parser.add_argument("--threshold", type=float, default=120.0,
                        help="max timestamp gap within one session in seconds (default: 120)")
    parser.add_argument("--duration", type=float, default=None,
                        help="limit each grid to this many seconds (quick previews)")
    parser.add_argument("--no-sync", action="store_true",
                        help="align all videos at t=0 instead of by filename timestamp")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="cell scale factor, e.g. 0.5 for 2880x1080 grids (default: 1.0)")
    parser.add_argument("--min-cameras", type=int, default=2,
                        help="skip sessions with fewer cameras than this (default: 2)")
    parser.add_argument("--crf", type=int, default=22, help="x264 quality (default: 22)")
    parser.add_argument("--preset", default="fast", help="x264 preset (default: fast)")
    parser.add_argument("-j", "--jobs", type=int, default=1,
                        help="render this many sessions in parallel (default: 1)")
    parser.add_argument("--force", action="store_true", help="re-render existing outputs")
    args = parser.parse_args()

    root = Path(__file__).parent
    per_camera = find_videos(root)
    sessions = group_sessions(per_camera, args.threshold)

    if args.group is not None:
        if not 0 <= args.group < len(sessions):
            print(f"session {args.group} not found ({len(sessions)} sessions)", file=sys.stderr)
            return 1
        selected = [(args.group, sessions[args.group])]
    else:
        selected = list(enumerate(sessions))

    if args.list:
        print(f"{len(sessions)} session(s), "
              f"{sum(len([k for k in s if k != '__ref__']) for s in sessions)} video(s):\n")
        for idx, session in selected:
            cams = {k: v for k, v in session.items() if k != "__ref__"}
            ref = min(v[1] for v in cams.values())
            print(f"[{idx:3d}] {ref:%Y-%m-%d %H:%M:%S}  cameras: {len(cams)}  "
                  f"missing: {', '.join(c for c in CAMERA_ORDER if c not in cams) or '-'}")
        return 0

    jobs = []
    for idx, session in selected:
        cams = {k: v for k, v in session.items() if k != "__ref__"}
        if len(cams) < args.min_cameras:
            print(f"[{idx}] skipped ({len(cams)} camera(s))")
            continue
        ref = min(v[1] for v in cams.values())
        output = args.output_dir / f"grid_{ref:%Y%m%d_%H%M%S}.mp4"
        if output.exists() and not args.force:
            print(f"[{idx}] exists, skipping ({output})")
            continue
        jobs.append((idx, cams, output))

    if not jobs:
        print("nothing to do")
        return 0

    print(f"Rendering {len(jobs)} grid video(s), cells {int(CELL_W * args.scale)}x"
          f"{int(CELL_H * args.scale)}, crf {args.crf}, preset {args.preset}\n")

    if args.jobs > 1:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
            list(pool.map(lambda j: render_grid(j[1], j[2], args.duration, args.no_sync,
                                                args.scale, args.crf, args.preset), jobs))
    else:
        for idx, cams, output in jobs:
            print(f"=== session {idx} ===")
            render_grid(cams, output, args.duration, args.no_sync, args.scale, args.crf, args.preset)
            print()

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

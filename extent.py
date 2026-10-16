#!/usr/bin/env python3
"""Stretch shortened video segments back to their real recording length.

The segments in folders like ``camera_launcher_front/`` were recorded in 5-minute
windows, but the cameras dropped frames, so a 5-minute recording only contains
~130-240 s of video.  When played back at 30 fps the footage therefore looks
speeded up.

For every readable video this script slows playback down so the result lasts
exactly ``--target`` seconds (default 300 s = 5 min).  All original frames are
kept; frames are duplicated as needed to keep a constant 30 fps output.  The
re-encoded videos are written to a new folder (default: ``<source>_5min``),
leaving the originals untouched.

Usage:
    uv run extent.py                              # process the two front-camera folders
    uv run extent.py folder1 folder2 ...          # process specific folders
    uv run extent.py --target 300 -j 6            # custom duration / parallel jobs
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v"}
DEFAULT_FOLDERS = ["camera_launcher_front", "camera_robot_front"]


def probe(path: Path) -> dict | None:
    """Return stream/duration info for a video, or None if it is unreadable."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        return None

    nb_frames = None
    if video.get("nb_frames") not in (None, "N/A"):
        nb_frames = int(video["nb_frames"])

    fps = 30.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    if rate and rate != "0/0":
        num, _, den = rate.partition("/")
        try:
            fps = float(num) / float(den or 1)
        except ZeroDivisionError:
            pass

    duration = None
    if nb_frames:
        duration = nb_frames / fps
    if not duration:
        try:
            duration = float(data["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            return None
    if not duration or duration <= 0:
        return None

    return {
        "duration": duration,
        "fps": fps,
        "nb_frames": nb_frames,
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def extend_video(src: Path, dst: Path, target: float, crf: int, preset: str) -> dict:
    """Re-encode ``src`` slowed down to exactly ``target`` seconds at ``dst``."""
    info = probe(src)
    if info is None:
        return {"src": src, "status": "skipped", "reason": "unreadable/corrupt (ffprobe failed)"}

    factor = target / info["duration"]
    filters = [f"setpts=PTS*{factor:.12g}", "fps=30"]

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    if info["has_audio"]:
        cmd += ["-vf", ",".join(filters), "-af", f"asetpts=PTS*{factor:.12g}"]
    else:
        cmd += ["-vf", ",".join(filters), "-an"]
    cmd += [
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
    ]
    if info["has_audio"]:
        cmd += ["-c:a", "aac", "-b:a", "128k"]
    cmd += ["-t", f"{target:.6f}", str(dst)]

    dst.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return {"src": src, "status": "failed", "reason": " | ".join(tail)}

    result = probe(dst)
    if result is None:
        return {"src": src, "status": "failed", "reason": "output unreadable"}
    return {
        "src": src,
        "dst": dst,
        "status": "ok",
        "duration": result["duration"],
        "source_duration": info["duration"],
        "speedup": factor,
        "elapsed": time.time() - started,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Slow shortened segments down to a target duration.")
    parser.add_argument("folders", nargs="*", default=DEFAULT_FOLDERS,
                        help=f"source folders to process (default: {' '.join(DEFAULT_FOLDERS)})")
    parser.add_argument("--target", type=float, default=300.0,
                        help="target duration in seconds (default: 300 = 5 min)")
    parser.add_argument("-o", "--output-root", default=None,
                        help="write results under this root, keeping folder names "
                             "(default: next to each source folder as <folder>_5min)")
    parser.add_argument("-j", "--jobs", type=int, default=6, help="parallel ffmpeg processes (default: 6)")
    parser.add_argument("--crf", type=int, default=18, help="x264 quality, lower is better (default: 18)")
    parser.add_argument("--preset", default="medium", help="x264 preset (default: medium)")
    parser.add_argument("--force", action="store_true", help="re-do outputs that already exist")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("error: ffmpeg/ffprobe not found in PATH", file=sys.stderr)
        return 1

    root = Path.cwd()
    tasks: list[tuple[Path, Path]] = []
    for folder in args.folders:
        src_dir = Path(folder)
        if not src_dir.is_dir():
            print(f"error: folder not found: {src_dir}", file=sys.stderr)
            return 1
        if args.output_root:
            out_dir = Path(args.output_root) / src_dir.name
        else:
            out_dir = src_dir.parent / f"{src_dir.name}_5min"
        for src in sorted(src_dir.iterdir()):
            if src.suffix.lower() not in VIDEO_EXTENSIONS or src.name.startswith("._"):
                continue
            tasks.append((src, out_dir / src.name))

    if not tasks:
        print("no video files found", file=sys.stderr)
        return 1

    print(f"Processing {len(tasks)} videos -> target {args.target:.0f}s "
          f"({args.jobs} parallel jobs, crf {args.crf}, preset {args.preset})")

    results: list[dict] = []
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futures = {}
        for src, dst in tasks:
            if dst.exists() and not args.force:
                existing = probe(dst)
                if existing and abs(existing["duration"] - args.target) < 0.5:
                    results.append({"src": src, "dst": dst, "status": "cached",
                                    "duration": existing["duration"]})
                    done += 1
                    continue
            futures[pool.submit(extend_video, src, dst, args.target, args.crf, args.preset)] = (src, dst)

        for future in concurrent.futures.as_completed(futures):
            res = future.result()
            results.append(res)
            done += 1
            if res["status"] == "ok":
                print(f"[{done}/{len(tasks)}] {res['src'].name}: "
                      f"{res['source_duration']:.1f}s -> {res['duration']:.1f}s "
                      f"({res['speedup']:.2f}x slower, {res['elapsed']:.1f}s)", flush=True)
            else:
                print(f"[{done}/{len(tasks)}] {res['src'].name}: {res['status'].upper()} "
                      f"- {res.get('reason', '')}", flush=True)

    ok = [r for r in results if r["status"] in ("ok", "cached")]
    problems = [r for r in results if r["status"] not in ("ok", "cached")]

    # Report
    report_lines = [f"extent.py report - target {args.target:.0f}s - {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    report_lines.append(f"processed: {len(ok)}/{len(results)}")
    for r in sorted(results, key=lambda r: str(r["src"])):
        if r["status"] in ("ok", "cached"):
            report_lines.append(
                f"  OK      {r['src'].parent.name}/{r['src'].name}: "
                f"{r.get('source_duration', r['duration']):.1f}s -> {r['duration']:.1f}s"
            )
        else:
            report_lines.append(f"  {r['status'].upper():7} {r['src'].parent.name}/{r['src'].name}: {r.get('reason', '')}")
    out_dirs = sorted({str(d.parent) for _, d in tasks})
    for d in out_dirs:
        Path(d, "extent_report.txt").write_text("\n".join(report_lines) + "\n")

    print(f"\nDone: {len(ok)} ok, {len(problems)} problem(s).")
    for r in problems:
        print(f"  {r['status']}: {r['src']} - {r.get('reason', '')}")
    print(f"Output folders: {', '.join(out_dirs)}")
    return 0 if not problems else 2


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Merge a folder of timestamped segments into one video, padding gaps with black.

Segments like ``camera_launcher_L/recording_20260917_092917_idx_2.mp4`` are
named by their start time and are supposed to be 5-minute windows, but the
cameras stop/start early, drop whole windows and occasionally produce a
corrupt file.  This script rebuilds the real wall-clock timeline:

1. Lists the video files (skipping ``._`` AppleDouble junk and the output file).
2. Sorts them by the timestamp in the filename and probes each one with
   ``ffprobe`` - corrupt/unreadable files are skipped and reported.
3. For every hole between "end of previous clip" and "start of next clip",
   generates a black placeholder clip of exactly that many frames.
4. Concatenates everything with ffmpeg's concat demuxer using **stream copy**
   (no quality loss, disk-speed only).  All segments must share codec,
   resolution, frame rate and time base - the script checks that.

Usage:
    uv run merge_with_gaps.py camera_launcher_L
    uv run merge_with_gaps.py camera_launcher_L -o merged.mp4 --dry-run
    uv run merge_with_gaps.py camera_launcher_L --reencode   # fallback if copy fails
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v"}
TIMESTAMP_RE = re.compile(r"(20\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")


def probe(path: Path) -> dict | None:
    """Return stream info for a video, or None if it is unreadable."""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path),
            ],
            capture_output=True, text=True, timeout=120,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return None

    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        return None

    fps = 30.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "30/1"
    if rate and rate != "0/0":
        try:
            fps = float(Fraction(rate))
        except (ValueError, ZeroDivisionError):
            pass
    if fps <= 0:
        return None

    nb_frames = None
    if video.get("nb_frames") not in (None, "N/A"):
        nb_frames = int(video["nb_frames"])

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

    time_base = video.get("time_base") or "1/15360"
    try:
        tb_num, tb_den = (int(x) for x in time_base.split("/"))
    except (ValueError, ZeroDivisionError):
        tb_num, tb_den = 1, 15360

    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)

    return {
        "duration": duration,
        "frames": nb_frames if nb_frames else round(duration * fps),
        "fps": fps,
        "fps_raw": rate,
        "width": video.get("width"),
        "height": video.get("height"),
        "pix_fmt": video.get("pix_fmt"),
        "codec": video.get("codec_name"),
        "profile": video.get("profile"),
        "time_base": (tb_num, tb_den),
        "audio": None
        if audio is None
        else {
            "codec": audio.get("codec_name"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
            "layout": audio.get("channel_layout"),
        },
    }


def parse_timestamp(name: str) -> datetime | None:
    m = TIMESTAMP_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def hms(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@dataclass
class Item:
    kind: str                 # "video" | "black"
    path: Path
    frames: int
    start: datetime | None = None
    note: str = ""


@dataclass
class Plan:
    items: list[Item] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fps: float = 30.0
    ref: dict | None = None
    total_frames: int = 0
    black_frames: int = 0


def build_plan(files: list[Path]) -> Plan:
    plan = Plan()

    probed: list[tuple[Path, datetime | None, dict]] = []
    for f in files:
        info = probe(f)
        if info is None:
            plan.skipped.append((f, "unreadable/corrupt (no moov or ffprobe failed)"))
            continue
        probed.append((f, parse_timestamp(f.name), info))

    if not probed:
        return plan

    # Uniformity check: stream copy only works when everything matches.
    ref = probed[0][2]
    plan.ref = ref
    plan.fps = ref["fps"]
    for f, _, info in probed[1:]:
        for key in ("codec", "width", "height", "pix_fmt", "fps_raw", "time_base"):
            if info[key] != ref[key]:
                plan.warnings.append(
                    f"{f.name}: {key}={info[key]!r} differs from {ref[key]!r} "
                    f"({probed[0][0].name}) - stream copy may glitch, consider --reencode"
                )
        if bool(info["audio"]) != bool(ref["audio"]):
            plan.warnings.append(f"{f.name}: audio stream presence differs from reference")

    # Timestamps missing -> fall back to filename order, no gap logic around them.
    has_ts = all(ts is not None for _, ts, _ in probed)
    if not has_ts:
        plan.warnings.append("some filenames carry no timestamp - gap detection disabled for them")
        probed.sort(key=lambda r: r[0].name)
    else:
        probed.sort(key=lambda r: r[1])

    prev_end: datetime | None = None
    for f, ts, info in probed:
        if ts is not None and prev_end is not None:
            gap = (ts - prev_end).total_seconds()
            gap_frames = round(gap * plan.fps)
            if gap_frames >= 1:
                plan.items.append(Item("black", Path(), gap_frames,
                                       start=prev_end,
                                       note=f"{prev_end:%H:%M:%S} -> {ts:%H:%M:%S}"))
                plan.black_frames += gap_frames
            elif gap_frames <= -int(plan.fps):
                plan.warnings.append(
                    f"{f.name}: overlaps previous clip by {-gap_frames / plan.fps:.1f}s"
                )
        plan.items.append(Item("video", f, info["frames"], start=ts))
        if ts is not None:
            prev_end = ts + timedelta(seconds=info["frames"] / plan.fps)

    plan.total_frames = sum(i.frames for i in plan.items)
    return plan


def trim_plan(plan: Plan, cutoff: float) -> None:
    """Keep only the first ``cutoff`` seconds of the plan.

    Black clips that would straddle the cutoff are shortened to fit; a video
    that straddles it is kept whole and cut by ffmpeg's ``-t`` at encode time.
    """
    fps = plan.fps
    kept: list[Item] = []
    acc = 0.0
    for item in plan.items:
        if acc >= cutoff:
            break
        remaining = cutoff - acc
        if item.frames / fps > remaining:
            if item.kind == "black":
                item.frames = max(1, round(remaining * fps))
                item.note += " (shortened for --duration)"
            kept.append(item)
            break
        kept.append(item)
        acc += item.frames / fps
    plan.items = kept
    plan.total_frames = sum(i.frames for i in plan.items)
    plan.black_frames = sum(i.frames for i in plan.items if i.kind == "black")


def make_black(dst: Path, info: dict, frames: int) -> str | None:
    """Encode an exact-length black clip matching ``info``; return error or None."""
    fps = info["fps"]
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=black:s={info['width']}x{info['height']}:r={info['fps_raw']}",
        "-frames:v", str(frames),
        "-c:v", "mpeg4", "-profile:v", "0", "-q:v", "2",
        "-pix_fmt", info["pix_fmt"] or "yuv420p",
        "-video_track_timescale", str(info["time_base"][1]),
        "-bf", "0",
    ]
    if info["audio"]:
        a = info["audio"]
        cmd += [
            "-f", "lavfi",
            "-i", f"anullsrc=channel_layout={a['layout'] or 'stereo'}:sample_rate={a['sample_rate'] or 44100}",
            "-shortest",
            "-c:a", a["codec"] or "aac",
        ]
    cmd.append(str(dst))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not dst.exists():
        return (proc.stderr or "ffmpeg failed").strip().splitlines()[-1] if proc.stderr else "ffmpeg failed"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge timestamped segments into one video, filling recording gaps with black.")
    parser.add_argument("folder", help="folder containing the recording_*.mp4 segments")
    parser.add_argument("-o", "--output", default=None,
                        help="output file (default: <folder>/merged.mp4)")
    parser.add_argument("--reencode", action="store_true",
                        help="re-encode to H.264 instead of stream copy (slower, safest fallback)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan (files, gaps) and exit without encoding")
    parser.add_argument("--duration", type=float, default=None, metavar="SECONDS",
                        help="only build the first N seconds (quick test, e.g. --duration 20)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing output")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("error: ffmpeg/ffprobe not found in PATH", file=sys.stderr)
        return 1

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"error: folder not found: {folder}", file=sys.stderr)
        return 1

    output = Path(args.output) if args.output else folder / "merged.mp4"
    output = output.resolve()
    default_output = (folder / "merged.mp4").resolve()

    files = sorted(
        p for p in folder.iterdir()
        if p.suffix.lower() in VIDEO_EXTENSIONS
        and not p.name.startswith("._")
        and p.resolve() not in (output, default_output)
    )
    if not files:
        print("error: no video files found", file=sys.stderr)
        return 1

    if output.exists() and not args.force and not args.dry_run:
        print(f"error: {output} exists (use --force to overwrite)", file=sys.stderr)
        return 1

    print(f"Scanning {len(files)} files in {folder} ...", flush=True)
    plan = build_plan(files)

    if not plan.items:
        print("error: no readable video files", file=sys.stderr)
        return 1

    usable_files = len([i for i in plan.items if i.kind == "video"])

    if args.duration:
        if args.duration <= 0:
            print("error: --duration must be > 0", file=sys.stderr)
            return 1
        trim_plan(plan, args.duration)
        print(f"(test mode: only the first {args.duration:g}s of the timeline are built)")

    ref = plan.ref or {}
    fps = plan.fps
    usable = [i for i in plan.items if i.kind == "video"]
    print(f"\nusable: {usable_files}/{len(files)}   codec: {ref.get('codec')} "
          f"{ref.get('width')}x{ref.get('height')} @ {fps:g} fps, pix_fmt {ref.get('pix_fmt')}")
    for path, reason in plan.skipped:
        print(f"  SKIPPED {path.name}: {reason}")
    for w in plan.warnings:
        print(f"  WARNING {w}")

    black_items = [i for i in plan.items if i.kind == "black"]
    if black_items:
        print(f"\nblack placeholders: {len(black_items)}, "
              f"total {hms(plan.black_frames / fps)}")
        for i in black_items:
            print(f"  BLACK {i.note}  ({i.frames / fps:.1f}s, {i.frames} frames)")
    else:
        print("\nno gaps found")

    total = plan.total_frames / fps
    expected = total
    if args.duration:
        expected = min(total, args.duration)
    note = f"  (test: first {args.duration:g}s)" if args.duration else ""
    print(f"\nsegments: {len(plan.items)}   expected output duration: {hms(expected)} "
          f"({expected / 3600:.2f} h){note}")

    if args.dry_run:
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="merge_black_"))
    list_file = tmp / "concat.txt"
    started = time.time()
    try:
        # 1. black placeholders
        idx = 0
        for item in plan.items:
            if item.kind != "black":
                continue
            idx += 1
            item.path = tmp / f"black_{idx:03d}.mp4"
            print(f"[black {idx}/{len(black_items)}] {item.note} -> {item.frames / fps:.1f}s",
                  flush=True)
            err = make_black(item.path, ref, item.frames)
            if err:
                print(f"error: black placeholder failed: {err}", file=sys.stderr)
                return 1

        # 2. concat list
        lines: list[str] = []
        for item in plan.items:
            lines.append(f"file '{item.path.resolve()}'")
        list_file.write_text("\n".join(lines) + "\n")

        # 3. concatenate
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(list_file)]
        if args.reencode:
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18",
                    "-pix_fmt", "yuv420p"]
            if ref.get("audio"):
                cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-c", "copy"]
        if args.duration:
            cmd += ["-t", f"{args.duration:.6f}"]
        cmd.append(str(output))

        print(f"\nConcatenating {len(plan.items)} segments -> {output}", flush=True)
        proc = subprocess.run(cmd)
        if proc.returncode != 0 or not output.exists():
            print("error: ffmpeg concat failed", file=sys.stderr)
            if not args.reencode:
                print("hint: retry with --reencode", file=sys.stderr)
            return 1

        # 4. verify
        out_info = probe(output)
        if out_info is None:
            print("error: output is unreadable", file=sys.stderr)
            return 1
        drift = abs(out_info["duration"] - expected)
        status = "OK" if drift < 1.0 else "MISMATCH"
        print(f"\n{status}: output {hms(out_info['duration'])}, expected {hms(expected)} "
              f"(drift {drift:.2f}s), {out_info['frames']} frames, "
              f"{output.stat().st_size / 1e9:.1f} GB, {time.time() - started:.0f}s")

        report = [
            f"merge_with_gaps.py report - {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"folder:  {folder}",
            f"output:  {output}",
            f"mode:    {'re-encode' if args.reencode else 'stream copy'}"
            + (f" (test: first {args.duration:g}s)" if args.duration else ""),
            f"files:   {len(files)} found, {usable_files} readable, {len(plan.skipped)} skipped",
            f"segments:{len(plan.items)} ({len(black_items)} black placeholders, "
            f"{hms(plan.black_frames / fps)} of black)",
            f"duration:{hms(out_info['duration'])} output / {hms(expected)} expected",
            "",
        ]
        for path, reason in plan.skipped:
            report.append(f"  SKIPPED {path.name}: {reason}")
        for item in black_items:
            report.append(f"  BLACK   {item.note}: {item.frames / fps:.1f}s")
        for w in plan.warnings:
            report.append(f"  WARNING {w}")
        if args.duration:
            print("(test mode: no merge_report.txt written)")
        else:
            report_path = folder / "merge_report.txt"
            report_path.write_text("\n".join(report) + "\n")
            print(f"report:  {report_path}")
        return 0 if status == "OK" else 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

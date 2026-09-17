#!/usr/bin/env python3
"""Merge video segments in a folder into one video.

Usage: python3 merge_videos.py <input_folder> [-o output.mp4]
"""

import argparse
import os
import subprocess
import sys
import tempfile

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".ts", ".m4v"}


def find_segments(folder):
    files = []
    for name in os.listdir(folder):
        if name.startswith("._"):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS:
            files.append(name)
    return sorted(files)


def is_valid_video(path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return False
        duration = float(result.stdout.strip())
        return duration > 0
    except (subprocess.TimeoutExpired, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description="Merge video segments into one video.")
    parser.add_argument("folder", help="Folder containing video segments")
    parser.add_argument("-o", "--output", default=None, help="Output file path")
    parser.add_argument(
        "--reencode",
        action="store_true",
        help="Re-encode instead of stream copy (slower, use if copy fails)",
    )
    args = parser.parse_args()

    folder = os.path.abspath(args.folder)
    if not os.path.isdir(folder):
        sys.exit(f"Error: not a folder: {folder}")

    output = args.output or os.path.join(folder, "merged.mp4")
    output = os.path.abspath(output)

    segments = find_segments(folder)
    if not segments:
        sys.exit("No video files found.")

    print(f"Found {len(segments)} segments. Validating...")
    valid = []
    for name in segments:
        path = os.path.join(folder, name)
        if os.path.abspath(path) == output:
            continue
        if is_valid_video(path):
            valid.append(path)
            print(f"  OK      {name}")
        else:
            print(f"  INVALID {name} (skipped)")

    if not valid:
        sys.exit("No valid videos to merge.")

    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        list_file = f.name
        for path in valid:
            escaped = path.replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
        if args.reencode:
            cmd += ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]
        else:
            cmd += ["-c", "copy"]
        cmd.append(output)

        print(f"Merging {len(valid)} videos -> {output}")
        result = subprocess.run(cmd)
        if result.returncode != 0 and not args.reencode:
            print("Stream copy failed. Retry with --reencode")
            sys.exit(1)
        print("Done.")
    finally:
        os.unlink(list_file)


if __name__ == "__main__":
    main()

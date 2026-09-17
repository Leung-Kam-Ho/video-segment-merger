# Camera Recordings

Tools for working with segmented camera recordings.

Folders like `camera_launcher_front/`, `camera_robot_front/`, `camera_rotor/`, etc. contain
video segments named like `recording_20260917_095346_idx_1.mp4`.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- `ffmpeg` and `ffprobe` (e.g. `brew install ffmpeg` on macOS)

## Tools

### 1. `merge_videos.py` — Concatenate segments per camera

Combines all segments in a single camera folder into one video.

```bash
uv run merge_videos.py <folder> [-o output.mp4] [--reencode]
```

Example:

```bash
uv run merge_videos.py camera_launcher_front
# creates camera_launcher_front/merged.mp4
```

#### What it does

1. Lists video files in the folder (`.mp4`, `.mov`, `.mkv`, `.avi`, `.ts`, `.m4v`),
   skipping metadata files starting with `._`.
2. Sorts segments by filename (chronological for the `recording_YYYYMMDD_HHMMSS_idx_N` format).
3. Validates each segment with `ffprobe` — corrupt or zero-duration files are skipped.
4. Merges the valid segments with ffmpeg's concat demuxer into `merged.mp4`
   (or the path given with `-o`).

#### Options

| Option | Description |
|--------|-------------|
| `-o`, `--output` | Output file path (default: `<folder>/merged.mp4`) |
| `--reencode` | Re-encode to H.264/AAC instead of stream copy. Slower, but needed if segments have mismatched codecs or parameters. |

If stream copy fails, re-run with `--reencode`.

Merge all camera folders:

```bash
for d in camera_*/; do uv run merge_videos.py "$d"; done
```

### 2. `group_similar_videos.py` — Find synchronized recordings

Groups videos across all camera folders by timestamp. Segments recorded within
60 seconds of each other are listed as a group.

```bash
uv run group_similar_videos.py
```

### 3. `merge_grid.py` — Merge 6 cameras into a 2x3 grid

Overlays synchronized recordings from all 6 cameras into a single 5760x2160 video.

Layout:

| Position | Camera |
|----------|--------|
| 1 (top-left) | robot_front |
| 2 (top-center) | launcher_front |
| 3 (top-right) | rotor |
| 4 (bottom-left) | stator |
| 5 (bottom-center) | launcher_L |
| 6 (bottom-right) | launcher_R |

```bash
# Merge time group 0 (auto-detected)
uv run merge_grid.py -g 0 -o grid.mp4

# Quick 10-second test
uv run merge_grid.py -g 0 --duration 10 -o grid_test.mp4

# Explicit files in camera order
uv run merge_grid.py v1.mp4 v2.mp4 v3.mp4 v4.mp4 v5.mp4 v6.mp4 -o grid.mp4
```

Missing cameras are replaced with black frames. Shorter videos are padded with
black frames to match the longest video in the group.

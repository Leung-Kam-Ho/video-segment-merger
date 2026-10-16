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

### 4. `extent.py` — Slow shortened segments back down to 5 minutes

Segments are written in 5-minute windows, but frame drops leave only ~130–240 s
of video per file, so playback looks speeded up. `extent.py` slows every video
down so it lasts exactly 5 minutes (300 s): all original frames are kept and
duplicated as needed for a constant 30 fps output. The originals are never
modified — results go to a new folder.

```bash
# Extend the two front-camera folders
uv run extent.py

# Specific folders / custom target duration
uv run extent.py camera_rotor camera_stator --target 300
```

Output is written next to each source folder as `<folder>_5min/` (override with
`-o <root>`) with the same filenames, plus an `extent_report.txt` listing what
was converted and what was skipped.

| Option | Description |
|--------|-------------|
| `--target` | Target duration in seconds (default: `300`) |
| `-o`, `--output-root` | Write results under this root instead of `<folder>_5min` |
| `-j`, `--jobs` | Parallel ffmpeg processes (default: `6`) |
| `--crf` / `--preset` | x264 quality/speed (default: `18` / `medium`) |
| `--force` | Re-encode even if a correct-length output already exists |

Corrupt files (e.g. truncated recordings without a `moov` atom) cannot be read
and are skipped and listed in the report. Already-finished outputs are skipped,
so an interrupted run can simply be re-run.

### 5. `sync_grid.py` — Timestamp-aligned 2x3 grid, one video per session

Clusters all six camera folders into recording sessions by filename timestamp
and renders one synchronized 5760x2160 grid video per session. The **earliest**
video of each session is the time reference: cameras that started later get
black frames prepended, cameras that stopped early get black frames appended,
and missing cameras show black for the whole grid.

Layout (robot_front and launcher_front in the center column):

| launcher_L | robot_front | launcher_R |
|------------|-------------|------------|
| rotor | launcher_front | stator |

Real-time versions from `extent.py` (`<camera>_5min/`) are used automatically
for the front cameras when available — otherwise the sped-up originals are
used and a warning is printed.

```bash
uv run sync_grid.py --list          # show the detected sessions
uv run sync_grid.py                 # render every session into grids/
uv run sync_grid.py -g 0            # render only session 0
uv run sync_grid.py -g 0 --duration 10   # quick 10 s preview of session 0
```

Outputs are named `grids/grid_<YYYYMMDD_HHMMSS>.mp4` after the earliest
timestamp in the session. Already-rendered sessions are skipped, so the job is
resumable.

| Option | Description |
|--------|-------------|
| `-g`, `--group` | Render only this session index (from `--list`) |
| `--list` | List detected sessions and exit |
| `-o`, `--output-dir` | Output folder (default: `grids/`) |
| `--threshold` | Max timestamp gap within a session, seconds (default: `120`) |
| `--duration` | Limit each grid to N seconds (previews) |
| `--no-sync` | Align all videos at t=0 instead of by timestamp |
| `--scale` | Cell scale, e.g. `0.5` for 2880x1080 grids (default: `1.0`) |
| `--min-cameras` | Skip sessions with fewer cameras (default: `2`) |
| `-j`, `--jobs` | Render N sessions in parallel (default: `1`) |
| `--crf` / `--preset` | x264 quality/speed (default: `22` / `fast`) |

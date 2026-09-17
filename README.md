# Camera Recordings

Tools for working with segmented camera recordings.

Folders like `camera_launcher_front/`, `camera_robot_front/`, `camera_rotor/`, etc. contain
video segments named like `recording_20260917_095346_idx_1.mp4`. Use `merge_videos.py`
to combine a folder of segments into a single video.

## Requirements

- Python 3.12+
- `ffmpeg` and `ffprobe` (e.g. `brew install ffmpeg` on macOS)

## Usage

```bash
python3 merge_videos.py <folder> [-o output.mp4] [--reencode]
```

Example:

```bash
python3 merge_videos.py camera_launcher_front
# creates camera_launcher_front/merged.mp4
```

### What it does

1. Lists video files in the folder (`.mp4`, `.mov`, `.mkv`, `.avi`, `.ts`, `.m4v`),
   skipping metadata files starting with `._`.
2. Sorts segments by filename (chronological for the `recording_YYYYMMDD_HHMMSS_idx_N` format).
3. Validates each segment with `ffprobe` — corrupt or zero-duration files are skipped.
4. Merges the valid segments with ffmpeg's concat demuxer into `merged.mp4`
   (or the path given with `-o`).

### Options

| Option | Description |
|--------|-------------|
| `-o`, `--output` | Output file path (default: `<folder>/merged.mp4`) |
| `--reencode` | Re-encode to H.264/AAC instead of stream copy. Slower, but needed if segments have mismatched codecs or parameters. |

If stream copy fails, re-run with `--reencode`.

## Merging all camera folders

```bash
for d in camera_*/; do python3 merge_videos.py "$d"; done
```

# SIFTR

A local Python tool for downloading YouTube videos, extracting frames, and shortlisting images for impact reports.

Designers use the local web UI to browse extracted frames, tag them with themes, and export shortlists.

## Project structure

```
siftr/
├── frames/        # Extracted images, organised by market/creator
├── exports/       # Final exported shortlists
├── logs/          # Harvest logs
├── config.yaml    # Runtime configuration
├── database.sqlite  # SQLite database (gitignored)
└── requirements.txt
```

## Setup

1. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Review `config.yaml` and adjust settings as needed.

## Configuration

Key settings in `config.yaml`:

| Setting | Default | Description |
|---|---|---|
| `download_delay_min` / `max` | 3 / 8 | Random delay (seconds) between downloads |
| `frame_extraction_method` | `scene_change` | `scene_change` or `uniform` |
| `uniform_sampling_interval` | 30 | Seconds between frames (uniform mode) |
| `min_frames_per_video` | 10 | Minimum frames to extract per video |
| `dedup_threshold` | 90 | Perceptual hash similarity threshold (%) |
| `min_resolution_width/height` | 1920 / 1080 | Minimum frame resolution to keep |
| `review_batch_size` | 30 | Frames shown per review batch |
| `nearby_frames_window` | 60 | Window (seconds) for grouping nearby frames |
| `available_themes` | economic, social, cultural | Themes available for tagging |

## Database

SQLite database at `database.sqlite` with two tables:

- **videos** — tracks downloaded videos (id, creator, market, title, URL, themes, date)
- **frames** — tracks extracted frames (id, video reference, timestamp, resolution, review status, file path)

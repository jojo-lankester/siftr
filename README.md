# SIFTR

SIFTR is a local Python/Flask tool for curating YouTube creator footage for impact reports. It downloads videos via yt-dlp, extracts key frames using scene-change detection and perceptual deduplication, and serves a browser UI where designers can browse frames by market and creator, shortlist the best ones, tag them with themes, and export a structured folder of images with a manifest CSV ready for reporting.

## Known constraints

- **360p output (current)** — YouTube's SABR streaming enforcement limits yt-dlp downloads to 360p in the current setup. The review UI uses these low-res frames for browsing. High-res capture (Step 7.5) is planned next using a Playwright headless browser to screenshot frames at full resolution directly from YouTube at the exact timecode — this is the approach Jack used previously.
- **No frame detail panel** — Step 6 (click-to-expand frame detail view) is deferred. Frames are browse-only in the grid for now.
- **No edit/delete in Manage UI** — the `/manage` page is add-only. Removing a creator or video currently requires editing `videos.yaml` directly.
- **Chrome cookies required** — yt-dlp uses your local Chrome cookie store for YouTube auth. Requires Chrome to be installed and logged in to a YouTube account.

## Test data note

The creators in `videos.yaml` (`Test Creator A`, `Test Creator B`) are placeholder entries used during development. Replace them with real creator channel URLs before use in production.

## Setup

1. Clone the repo and create a virtual environment:
   ```bash
   git clone <repo-url> && cd siftr
   python3 -m venv .venv
   source .venv/bin/activate
   ```

2. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Install ffmpeg (required for frame extraction):
   ```bash
   # macOS
   brew install ffmpeg
   # Ubuntu/Debian
   sudo apt install ffmpeg
   ```

4. (Optional) Run the harvester to download and extract frames from the videos in `videos.yaml`:
   ```bash
   python harvest.py
   ```

5. Start the app:
   ```bash
   python app.py
   ```

6. Open **http://localhost:5001** in your browser.

## Project structure

```
siftr/
├── app.py              # Flask app — routes, DB, export logic
├── harvest.py          # Orchestrator — download + extract pipeline
├── download.py         # yt-dlp wrapper
├── extract_frames.py   # FFmpeg frame extraction + dedup
├── videos.yaml         # Market → creator → video URL config
├── config.yaml         # Runtime settings (thresholds, resolution, etc.)
├── frames/             # Extracted images, organised by market/creator/frame_id
├── exports/            # Export folders (one per export run)
├── logs/               # Harvest logs
├── static/             # CSS and JS
├── templates/          # Jinja2 HTML templates
└── database.sqlite     # SQLite DB (gitignored)
```

## Configuration

Key settings in `config.yaml`:

| Setting | Default | Description |
|---|---|---|
| `uniform_sampling_interval` | 30s | Seconds between frames in uniform fallback mode |
| `min_frames_per_video` | 10 | Min scene-change frames before falling back to uniform |
| `dedup_threshold` | 90% | Perceptual hash similarity threshold |
| `min_resolution_width/height` | 640 / 360 | Minimum frame resolution to keep (DEV MODE — raise to 1920/1080 for production) |
| `scene_change_threshold` | 0.3 | FFmpeg scene-change sensitivity |

## Workflow

1. Add creators and videos via the **Manage** page (`/manage`) or by editing `videos.yaml` directly
2. Run `python harvest.py` to download videos and extract frames (or trigger it from the Manage page)
3. Open a market in the browser, browse frames, and click to shortlist
4. Tag videos with themes using the chip editor on each video block
5. Switch to the **Shortlisted** filter and click **Export** to generate a folder of images + `manifest.csv`

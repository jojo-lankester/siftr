# SIFTR

SIFTR is a local Python/Flask tool for curating YouTube creator footage for impact reports. It downloads videos via yt-dlp, extracts key frames using scene-change detection and perceptual deduplication, and serves a browser UI where designers can browse frames by market and creator, shortlist the best ones, and tag them with themes.

---

## Workflow

1. **Harvest** — SIFTR downloads videos and extracts frames automatically (scene-change detection + dedup).
2. **Review** — Designers browse frames in the grid and shortlist the ones they want.
3. **Get the high-res image** — Open the **Watch on YouTube** button in the frame detail panel. YouTube opens at the right moment (there may be ±0.5–1s drift from encoding boundaries). Scrub to the exact frame and take a screenshot from there.

SIFTR's job is helping designers identify and shortlist the right frames. YouTube is the source for final high-res images.

---

## Setup on a fresh machine

1. Clone the repo:
   ```bash
   git clone <repo-url>
   cd siftr
   ```

2. Run the setup script:
   ```bash
   ./install.sh
   ```

3. Follow any prompts — if FFmpeg is missing, the script will tell you how to install it (`brew install ffmpeg`), then re-run `./install.sh` once it's done.

That's it. Then:

```bash
source .venv/bin/activate
python app.py
```

Open **http://localhost:5001** in your browser.

---

## What to do next

1. Add creators and videos via the **Manage** page (`/manage`) or by editing `videos.yaml` directly
2. Run `python harvest.py` to download videos and extract frames (or trigger it from the Manage page)
3. Open a market in the browser, browse frames, and click to shortlist
4. Tag videos with themes using the chip editor on each video block
5. For any shortlisted frame, click **Expand** → **Watch on YouTube** to open it at the right moment and screenshot at full resolution

---

## Known constraints

- **360p output (current)** — YouTube's SABR streaming enforcement limits yt-dlp downloads to 360p in the current setup. The review UI uses these low-res frames for browsing. Final high-res images come from YouTube directly via the Watch on YouTube link.
- **Chrome cookies required for download** — yt-dlp uses your local Chrome cookie store for YouTube auth. Chrome must be installed and logged in to a YouTube account on the machine running the harvester.

## Test data note

The creators in `videos.yaml` (`Test Creator A`, `Test Creator B`) are placeholder entries used during development. Replace them with real creator channel URLs before use in production.

---

## Project structure

```
siftr/
├── app.py              # Flask app — routes, DB, review logic
├── harvest.py          # Orchestrator — download + extract pipeline
├── download.py         # yt-dlp wrapper
├── extract_frames.py   # FFmpeg frame extraction + dedup
├── setup.py            # DB initialisation (run once)
├── install.sh          # First-time setup script
├── videos.yaml         # Market → creator → video URL config
├── config.yaml         # Runtime settings (thresholds, resolution, etc.)
├── frames/             # Extracted images, organised by market/creator/frame_id
├── logs/               # Harvest logs
├── static/             # CSS and JS
├── templates/          # Jinja2 HTML templates
└── database.sqlite     # SQLite DB (gitignored)
```

---

## Configuration

Key settings in `config.yaml`:

| Setting | Default | Description |
|---|---|---|
| `uniform_sampling_interval` | 30s | Seconds between frames in uniform fallback mode |
| `min_frames_per_video` | 10 | Min scene-change frames before falling back to uniform |
| `dedup_threshold` | 90% | Perceptual hash similarity threshold |
| `min_resolution_width/height` | 640 / 360 | Minimum frame resolution (DEV MODE — raise to 1920/1080 for production) |
| `scene_change_threshold` | 0.3 | FFmpeg scene-change sensitivity |

---

## Manual setup (if install.sh doesn't work)

These are the individual steps `install.sh` performs, in order:

1. **Check Python 3.10+** is installed on the system

2. **Create a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install Python dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Install FFmpeg** (used for frame extraction):
   ```bash
   # macOS
   brew install ffmpeg
   # Ubuntu/Debian
   sudo apt install ffmpeg
   ```

5. **Initialise the database:**
   ```bash
   python setup.py
   ```
   This creates `database.sqlite` with the full schema. Safe to run multiple times.

6. **Start the app:**
   ```bash
   python app.py
   ```
   Open **http://localhost:5001** in your browser.

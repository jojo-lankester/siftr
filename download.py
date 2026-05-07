"""
YouTube download module for SIFTR.

Reads URLs from videos.txt, skips any already in the database,
downloads each video (video-only, highest quality) via yt-dlp,
records metadata in the videos table, and logs a run summary.

Usage:
    python download.py                  # harvest from videos.txt
    python download.py --retry-failed   # retry URLs in logs/failed_urls.txt
"""

from __future__ import annotations

import argparse
import logging
import random
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
import yt_dlp

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
VIDEOS_TXT = BASE_DIR / "videos.txt"
DB_PATH = BASE_DIR / "database.sqlite"
DOWNLOAD_DIR = BASE_DIR / "frames" / "_raw_downloads"
LOG_PATH = BASE_DIR / "logs" / "harvest.log"
FAILED_URLS_PATH = BASE_DIR / "logs" / "failed_urls.txt"

RETRY_DELAYS = [30, 60]  # seconds between attempt 1→2 and 2→3


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("siftr.download")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = _setup_logging()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# URL / ID helpers
# ---------------------------------------------------------------------------

def extract_video_id(url: str) -> str | None:
    """Return the YouTube video ID from a URL, or None if not parseable."""
    parsed = urlparse(url)

    # Standard watch URLs: ?v=...
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        qs = parse_qs(parsed.query)
        ids = qs.get("v")
        if ids:
            return ids[0]

    # Short URLs: youtu.be/<id>
    if parsed.hostname == "youtu.be":
        path_part = parsed.path.lstrip("/").split("/")[0]
        if path_part:
            return path_part

    return None


def read_urls(path: Path) -> list[str]:
    """Return non-empty, non-comment lines from a URL list file."""
    if not path.exists():
        log.warning("URL file not found: %s", path)
        return []
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


# ---------------------------------------------------------------------------
# Failed-URL tracking
# ---------------------------------------------------------------------------

def append_failed_url(url: str, error: str) -> None:
    """Append a failed URL with its error and timestamp to failed_urls.txt."""
    FAILED_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(FAILED_URLS_PATH, "a", encoding="utf-8") as f:
        f.write(f"# Failed: {timestamp} | {error}\n")
        f.write(f"{url}\n")


def clear_failed_urls() -> None:
    """Truncate failed_urls.txt at the start of a retry run."""
    if FAILED_URLS_PATH.exists():
        FAILED_URLS_PATH.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_existing_ids(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT video_id FROM videos")
    return {row[0] for row in cur.fetchall()}


def insert_video(conn: sqlite3.Connection, video_id: str, url: str, title: str) -> None:
    conn.execute(
        """
        INSERT INTO videos (video_id, video_url, video_title, date_added,
                            creator_name, market, themes)
        VALUES (?, ?, ?, ?, '', '', '')
        """,
        (video_id, url, title, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def count_videos(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]


# ---------------------------------------------------------------------------
# Download (with retry)
# ---------------------------------------------------------------------------

def download_video(url: str, video_id: str, dest_dir: Path,
                   cookies_browser: str | None = None,
                   player_client: str | None = None) -> dict | None:
    """
    Download the best available stream for *url* into *dest_dir*.
    Returns yt-dlp's info dict on success, raises on failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        # Prefer video-only; fall back to best combined stream if YouTube
        # blocks separate streams (e.g. SABR streaming enforcement).
        "format": "bestvideo[ext=mp4]/bestvideo/best[ext=mp4]/best",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": False,
        "nooverwrites": True,
    }

    if cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser,)

    if player_client:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info


def download_with_retry(url: str, video_id: str, dest_dir: Path,
                        cookies_browser: str | None,
                        player_client: str | None,
                        position: str) -> dict | None:
    """
    Attempt download up to 1 + len(RETRY_DELAYS) times.
    Returns info dict on success, or None if all attempts fail (after logging
    each attempt and appending the URL to failed_urls.txt).
    """
    max_attempts = 1 + len(RETRY_DELAYS)
    last_error: Exception | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            if attempt > 1:
                log.info("%s RETRY %d/%d  %s", position, attempt, max_attempts, video_id)
            info = download_video(url, video_id, dest_dir, cookies_browser, player_client)
            return info
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts:
                wait = RETRY_DELAYS[attempt - 1]
                log.warning("%s attempt %d failed: %s — retrying in %ds",
                            position, attempt, exc, wait)
                time.sleep(wait)
            else:
                log.error("%s FAIL (all %d attempts)  %s  —  %s",
                          position, max_attempts, video_id, exc)

    append_failed_url(url, str(last_error))
    return None


# ---------------------------------------------------------------------------
# Main harvest
# ---------------------------------------------------------------------------

def run_harvest(url_source: Path) -> None:
    log.info("=" * 60)
    if url_source == FAILED_URLS_PATH:
        log.info("SIFTR harvest started  [retrying failed URLs]")
    else:
        log.info("SIFTR harvest started")

    cfg = load_config()
    delay_min = cfg.get("download_delay_min", 3)
    delay_max = cfg.get("download_delay_max", 8)
    cookies_browser = cfg.get("cookies_browser") or None
    player_client = cfg.get("youtube_player_client") or None

    urls = read_urls(url_source)
    log.info("Found %d URL(s) in %s", len(urls), url_source.name)

    # Clear failed_urls.txt before a retry run so it only reflects this run's results
    if url_source == FAILED_URLS_PATH:
        clear_failed_urls()

    conn = sqlite3.connect(DB_PATH)
    existing_ids = get_existing_ids(conn)
    log.info("Database already contains %d video(s)", len(existing_ids))

    processed = 0
    skipped = 0
    failures = 0

    for idx, url in enumerate(urls, start=1):
        position = f"[{idx}/{len(urls)}]"
        video_id = extract_video_id(url)

        if not video_id:
            log.warning("%s Cannot parse video ID from URL: %s", position, url)
            append_failed_url(url, "Could not parse video ID")
            failures += 1
            continue

        if video_id in existing_ids:
            log.info("%s SKIP  %s (already in database)", position, video_id)
            skipped += 1
            continue

        log.info("%s START %s  —  %s", position, video_id, url)

        info = download_with_retry(url, video_id, DOWNLOAD_DIR,
                                   cookies_browser, player_client, position)
        if info is not None:
            title = info.get("title", "")
            insert_video(conn, video_id, url, title)
            log.info('%s OK    %s  —  "%s"', position, video_id, title)
            processed += 1
        else:
            failures += 1

        # Polite delay between videos (skip after the last one)
        if idx < len(urls):
            delay = random.uniform(delay_min, delay_max)
            log.debug("Waiting %.1fs before next download", delay)
            time.sleep(delay)

    total_in_db = count_videos(conn)
    conn.close()

    log.info("-" * 60)
    log.info("Run summary:")
    log.info("  Processed (new downloads) : %d", processed)
    log.info("  Skipped (already in DB)   : %d", skipped)
    log.info("  Failures                  : %d", failures)
    log.info("  Total videos in database  : %d", total_in_db)
    if failures and FAILED_URLS_PATH.exists() and FAILED_URLS_PATH.stat().st_size > 0:
        log.info("  Failed URLs logged to     : %s", FAILED_URLS_PATH)
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SIFTR YouTube harvest")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry URLs from logs/failed_urls.txt instead of videos.txt",
    )
    args = parser.parse_args()

    source = FAILED_URLS_PATH if args.retry_failed else VIDEOS_TXT
    run_harvest(source)

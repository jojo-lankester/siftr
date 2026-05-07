"""
YouTube download module for SIFTR.

Reads URLs from videos.txt, skips any already in the database,
downloads each video (video-only, highest quality) via yt-dlp,
records metadata in the videos table, and logs a run summary.

Usage:
    python download.py
"""

import logging
import os
import random
import re
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
    """Return non-empty, non-comment lines from videos.txt."""
    if not path.exists():
        log.warning("videos.txt not found at %s", path)
        return []
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


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
# Download
# ---------------------------------------------------------------------------

def download_video(url: str, video_id: str, dest_dir: Path) -> dict | None:
    """
    Download the best video-only stream for *url* into *dest_dir*.
    Returns yt-dlp's info dict on success, None on failure.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    ydl_opts = {
        # Best video-only stream, no audio
        "format": "bestvideo[ext=mp4]/bestvideo",
        "outtmpl": str(dest_dir / "%(id)s.%(ext)s"),
        # Suppress yt-dlp's own stdout chatter; we log ourselves
        "quiet": True,
        "no_warnings": False,
        # Don't re-download if the file already exists
        "nooverwrites": True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info


# ---------------------------------------------------------------------------
# Main harvest
# ---------------------------------------------------------------------------

def run_harvest() -> None:
    log.info("=" * 60)
    log.info("SIFTR harvest started")

    cfg = load_config()
    delay_min = cfg.get("download_delay_min", 3)
    delay_max = cfg.get("download_delay_max", 8)

    urls = read_urls(VIDEOS_TXT)
    log.info("Found %d URL(s) in videos.txt", len(urls))

    conn = sqlite3.connect(DB_PATH)
    existing_ids = get_existing_ids(conn)
    log.info("Database already contains %d video(s)", len(existing_ids))

    processed = 0
    skipped = 0
    failures = 0

    for idx, url in enumerate(urls, start=1):
        video_id = extract_video_id(url)

        if not video_id:
            log.warning("[%d/%d] Cannot parse video ID from URL: %s", idx, len(urls), url)
            failures += 1
            continue

        if video_id in existing_ids:
            log.info("[%d/%d] SKIP  %s (already in database)", idx, len(urls), video_id)
            skipped += 1
            continue

        log.info("[%d/%d] START %s  —  %s", idx, len(urls), video_id, url)

        try:
            info = download_video(url, video_id, DOWNLOAD_DIR)
            title = info.get("title", "") if info else ""
            insert_video(conn, video_id, url, title)
            log.info("[%d/%d] OK    %s  —  \"%s\"", idx, len(urls), video_id, title)
            processed += 1
        except Exception as exc:
            log.error("[%d/%d] FAIL  %s  —  %s", idx, len(urls), video_id, exc)
            failures += 1

        # Polite delay between downloads (skip after the last URL)
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
    log.info("=" * 60)


if __name__ == "__main__":
    run_harvest()

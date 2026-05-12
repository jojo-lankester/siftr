"""
YouTube download module for SIFTR.

Reads video metadata from videos.yaml (market → creator → videos),
skips any video_id already in the database, downloads each new video
via yt-dlp, records full metadata in the videos table, and logs a
run summary.

Usage:
    python download.py                  # harvest from videos.yaml
    python download.py --retry-failed   # retry URLs in logs/failed_urls.txt

KNOWN LIMITATION — 360p output (SABR streaming):
    YouTube's SABR streaming enforcement means yt-dlp currently only receives
    360p streams for most videos, regardless of what quality is available on
    the site. The android player_client workaround bypasses SABR but doesn't
    support cookies; the web client supports cookies but is SABR-restricted.
    Before production use, this needs to be resolved — most likely by
    configuring a PO Token (Proof of Origin token) from an authenticated
    Chrome session. See: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
"""

from __future__ import annotations

import argparse
import logging
import platform
import random
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import yaml
import yt_dlp

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
VIDEOS_YAML = BASE_DIR / "videos.yaml"
DB_PATH = BASE_DIR / "database.sqlite"
DOWNLOAD_DIR = BASE_DIR / "frames" / "_raw_downloads"
LOG_PATH = BASE_DIR / "logs" / "harvest.log"
FAILED_URLS_PATH = BASE_DIR / "logs" / "failed_urls.txt"

RETRY_DELAYS = [30, 60]  # seconds between retries for genuine failures

# ---------------------------------------------------------------------------
# Browser availability check
# ---------------------------------------------------------------------------

# Common CLI executable names per browser (Linux / macOS PATH)
_BROWSER_CLI_NAMES: dict[str, list[str]] = {
    "chrome":   ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"],
    "chromium": ["chromium", "chromium-browser", "google-chrome"],
    "firefox":  ["firefox", "firefox-esr"],
    "edge":     ["microsoft-edge", "msedge"],
    "brave":    ["brave", "brave-browser"],
    "opera":    ["opera"],
    "safari":   [],  # macOS built-in; detected via app bundle below
}

# macOS application bundle paths
_BROWSER_MAC_APPS: dict[str, str] = {
    "chrome":   "/Applications/Google Chrome.app",
    "chromium": "/Applications/Chromium.app",
    "firefox":  "/Applications/Firefox.app",
    "edge":     "/Applications/Microsoft Edge.app",
    "brave":    "/Applications/Brave Browser.app",
    "safari":   "/Applications/Safari.app",
    "opera":    "/Applications/Opera.app",
}


def _browser_available(browser: str) -> bool:
    """Return True if the named browser appears to be installed on this machine."""
    b = browser.lower()
    if platform.system() == "Darwin":
        app_path = _BROWSER_MAC_APPS.get(b)
        if app_path and Path(app_path).exists():
            return True
    for name in _BROWSER_CLI_NAMES.get(b, [b]):
        if shutil.which(name):
            return True
    return False


# ---------------------------------------------------------------------------
# Auth-error heuristic
# ---------------------------------------------------------------------------

# Substrings that suggest a YouTube auth/access failure (cookies may help)
_AUTH_SIGNALS = ("403", "sign in", "sign-in", "login", "age-restrict", "members only")


def _looks_like_auth_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(s in msg for s in _AUTH_SIGNALS)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("siftr.download")
    if logger.handlers:
        return logger
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
    if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
        ids = parse_qs(parsed.query).get("v")
        if ids:
            return ids[0]
    if parsed.hostname == "youtu.be":
        part = parsed.path.lstrip("/").split("/")[0]
        if part:
            return part
    return None


def read_urls(path: Path) -> list[str]:
    """Return non-empty, non-comment lines from a plain URL list file."""
    if not path.exists():
        log.warning("File not found: %s", path)
        return []
    urls = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    return urls


# ---------------------------------------------------------------------------
# videos.yaml parsing
# ---------------------------------------------------------------------------

def load_video_entries() -> list[dict]:
    """
    Parse videos.yaml into a flat list of video entry dicts.
    Each dict has: url, video_id, market, creator_name, creator_slug, themes.
    Themes inherit from creator default_themes unless overridden per-video.
    """
    if not VIDEOS_YAML.exists():
        log.error("videos.yaml not found at %s", VIDEOS_YAML)
        return []
    with open(VIDEOS_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f)

    entries: list[dict] = []
    for market in data.get("markets", []):
        code = market["code"]
        for creator in market.get("creators", []):
            name = creator["name"]
            slug = creator["creator_slug"]
            defaults = creator.get("default_themes", [])
            for video in creator.get("videos", []):
                url = video["url"]
                video_id = extract_video_id(url)
                if not video_id:
                    log.warning("Cannot parse video_id from URL: %s", url)
                    continue
                themes = video.get("themes", defaults)
                if isinstance(themes, list):
                    themes = ",".join(themes)
                entries.append({
                    "url": url,
                    "video_id": video_id,
                    "market": code,
                    "creator_name": name,
                    "creator_slug": slug,
                    "themes": themes,
                })
    return entries


def build_id_to_meta(entries: list[dict]) -> dict[str, dict]:
    """Index video entries by video_id for fast lookup during retry runs."""
    return {e["video_id"]: e for e in entries}


# ---------------------------------------------------------------------------
# Failed-URL tracking
# ---------------------------------------------------------------------------

def append_failed_url(url: str, error: str) -> None:
    FAILED_URLS_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    with open(FAILED_URLS_PATH, "a", encoding="utf-8") as f:
        f.write(f"# Failed: {timestamp} | {error}\n")
        f.write(f"{url}\n")


def clear_failed_urls() -> None:
    if FAILED_URLS_PATH.exists():
        FAILED_URLS_PATH.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add creator_slug column to videos table if not already present."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(videos)")}
    if "creator_slug" not in existing:
        conn.execute("ALTER TABLE videos ADD COLUMN creator_slug TEXT DEFAULT ''")
        conn.commit()
        log.debug("Schema: added creator_slug column to videos table")


def get_existing_ids(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT video_id FROM videos")}


def insert_video(conn: sqlite3.Connection, video_id: str, url: str,
                 title: str, meta: dict) -> None:
    conn.execute(
        """
        INSERT INTO videos
          (video_id, video_url, video_title, date_added,
           creator_name, creator_slug, market, themes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            video_id, url, title,
            datetime.now(timezone.utc).isoformat(),
            meta.get("creator_name", ""),
            meta.get("creator_slug", ""),
            meta.get("market", ""),
            meta.get("themes", ""),
        ),
    )
    conn.commit()


def update_video_metadata(conn: sqlite3.Connection, video_id: str, meta: dict) -> None:
    """Backfill creator/market/themes for a video already in the database."""
    conn.execute(
        """
        UPDATE videos
        SET creator_name = ?, creator_slug = ?, market = ?, themes = ?
        WHERE video_id = ?
        """,
        (
            meta.get("creator_name", ""),
            meta.get("creator_slug", ""),
            meta.get("market", ""),
            meta.get("themes", ""),
            video_id,
        ),
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
    dest_dir.mkdir(parents=True, exist_ok=True)
    ydl_opts = {
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
        return ydl.extract_info(url, download=True)


def download_with_retry(url: str, video_id: str, dest_dir: Path,
                        cookies_browser: str | None,
                        player_client: str | None,
                        position: str) -> dict | None:
    """
    Download strategy:
      1. Try without cookies (fast; works for most public videos).
      2. If that fails with an auth-related error AND a cookies browser is
         configured, try once with cookies — no delay before this step.
      3. For any remaining failures, retry with RETRY_DELAYS between attempts.
         Retries use cookies only if the auth escalation was triggered.
    """
    last_error: Exception | None = None
    use_cookies_for_retries: str | None = None

    # Attempt 1 — no cookies
    try:
        log.debug("%s attempt 1: no cookies", position)
        return download_video(url, video_id, dest_dir, None, player_client)
    except Exception as exc:
        last_error = exc
        log.debug("%s attempt 1 failed: %s", position, exc)

    # Attempt 2 — cookie escalation (no delay; only for auth-like errors)
    if _looks_like_auth_error(last_error) and cookies_browser:
        log.info("%s auth error on first attempt — retrying with %s cookies",
                 position, cookies_browser)
        try:
            return download_video(url, video_id, dest_dir, cookies_browser, player_client)
        except Exception as exc:
            last_error = exc
            use_cookies_for_retries = cookies_browser
            log.debug("%s cookie attempt failed: %s", position, exc)

    # Remaining attempts — with delays (genuine failure retries)
    for i, wait in enumerate(RETRY_DELAYS, start=3):
        log.warning("%s attempt %d failed: %s — retrying in %ds",
                    position, i - 1, last_error, wait)
        time.sleep(wait)
        try:
            return download_video(url, video_id, dest_dir,
                                  use_cookies_for_retries, player_client)
        except Exception as exc:
            last_error = exc

    log.error("%s FAIL (all attempts exhausted)  %s  —  %s",
              position, video_id, last_error)
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

    if cookies_browser and not _browser_available(cookies_browser):
        log.warning(
            "Configured cookies browser '%s' not found — proceeding without cookies. "
            "If downloads fail, install the browser or set cookies_browser to null "
            "in config.yaml.",
            cookies_browser,
        )
        cookies_browser = None

    # Always load the full YAML so we have metadata for retry runs too
    all_entries = load_video_entries()
    id_to_meta = build_id_to_meta(all_entries)

    if url_source == FAILED_URLS_PATH:
        clear_failed_urls()
        failed_urls = read_urls(FAILED_URLS_PATH)
        entries = []
        for url in failed_urls:
            vid_id = extract_video_id(url)
            meta = id_to_meta.get(vid_id, {}) if vid_id else {}
            entries.append({
                "url": url,
                "video_id": vid_id,
                "market": meta.get("market", ""),
                "creator_name": meta.get("creator_name", ""),
                "creator_slug": meta.get("creator_slug", ""),
                "themes": meta.get("themes", ""),
            })
    else:
        entries = all_entries

    log.info("Found %d video(s) to process", len(entries))

    conn = sqlite3.connect(DB_PATH)
    migrate_schema(conn)
    existing_ids = get_existing_ids(conn)
    log.info("Database already contains %d video(s)", len(existing_ids))

    processed = skipped = failures = 0

    for idx, entry in enumerate(entries, start=1):
        position = f"[{idx}/{len(entries)}]"
        url = entry["url"]
        video_id = entry.get("video_id") or extract_video_id(url)

        if not video_id:
            log.warning("%s Cannot parse video_id from: %s", position, url)
            append_failed_url(url, "Could not parse video ID")
            failures += 1
            continue

        if video_id in existing_ids:
            update_video_metadata(conn, video_id, entry)
            log.info("%s SKIP  %s (already in DB, metadata updated)", position, video_id)
            skipped += 1
            continue

        log.info("%s START %s  —  %s", position, video_id, url)
        info = download_with_retry(url, video_id, DOWNLOAD_DIR,
                                   cookies_browser, player_client, position)
        if info is not None:
            title = info.get("title", "")
            insert_video(conn, video_id, url, title, entry)
            log.info('%s OK    %s  —  "%s"', position, video_id, title)
            processed += 1
        else:
            failures += 1

        if idx < len(entries):
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
        help="Retry URLs from logs/failed_urls.txt instead of videos.yaml",
    )
    args = parser.parse_args()
    source = FAILED_URLS_PATH if args.retry_failed else VIDEOS_YAML
    run_harvest(source)

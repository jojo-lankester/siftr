"""
Frame extraction module for SIFTR.

For each video in the database that has no frames yet, extracts frames
using FFmpeg scene-change detection (with uniform sampling as fallback),
applies resolution and deduplication filters, saves frames to disk, and
records each surviving frame in the frames table.

Usage:
    python extract_frames.py
"""

from __future__ import annotations

import logging
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import imagehash
import yaml
from PIL import Image

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
DB_PATH = BASE_DIR / "database.sqlite"
DOWNLOAD_DIR = BASE_DIR / "frames" / "_raw_downloads"
FRAMES_DIR = BASE_DIR / "frames"
LOG_PATH = BASE_DIR / "logs" / "harvest.log"

# ffmpeg scene-change score (0.0–1.0); 0.3 is a good general-purpose value
SCENE_THRESHOLD = 0.3


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("siftr.extract")
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
# Config & pre-flight
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def check_ffmpeg() -> None:
    """Exit with a clear message if ffmpeg is not on PATH."""
    if not shutil.which("ffmpeg"):
        log.error(
            "ffmpeg not found on PATH. Install it first:\n"
            "  macOS:   brew install ffmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg\n"
            "  Windows: https://ffmpeg.org/download.html"
        )
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(text: str) -> str:
    """Convert a string to a lowercase, filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text or "unknown"


def seconds_to_timecode(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def seconds_to_filename_ts(seconds: float) -> str:
    """Format timestamp for use in filenames: HH-MM-SS.mmm (no colons)."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}-{m:02d}-{s:06.3f}"


def find_video_file(video_id: str) -> Path | None:
    """Return the downloaded video file path regardless of extension."""
    matches = [
        p for p in DOWNLOAD_DIR.glob(f"{video_id}.*")
        if p.suffix.lower() not in (".part", ".ytdl")
    ]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# FFmpeg extraction
# ---------------------------------------------------------------------------

def extract_scene_frames(video_path: Path, temp_dir: Path) -> list[tuple[float, Path]]:
    """
    Extract frames at scene changes using FFmpeg's select + showinfo filters.
    Returns list of (timestamp_seconds, temp_frame_path) pairs.
    """
    out_pattern = str(temp_dir / "scene_%06d.jpg")
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", rf"select=gt(scene\,{SCENE_THRESHOLD}),showinfo",
        "-vsync", "vfr",
        "-q:v", "2",
        out_pattern,
        "-y",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)

    # showinfo writes to stderr; parse n: (0-indexed frame counter) and pts_time:
    # Example line: [Parsed_showinfo_1 @ 0x...] n:   0 pts:      0 pts_time:0 ...
    n_ts_re = re.compile(r"n:\s*(\d+)\s+pts:\s*\d+\s+pts_time:(\S+)")
    index_to_ts: dict[int, float] = {}
    for line in result.stderr.splitlines():
        if "Parsed_showinfo" in line:
            m = n_ts_re.search(line)
            if m:
                try:
                    index_to_ts[int(m.group(1))] = float(m.group(2))
                except ValueError:
                    pass

    frame_files = sorted(temp_dir.glob("scene_*.jpg"))
    pairs = []
    for i, f in enumerate(frame_files):
        ts = index_to_ts.get(i, float(i * 5))  # fallback if parse fails
        pairs.append((ts, f))
    return pairs


def extract_uniform_frames(
    video_path: Path, temp_dir: Path, interval: int
) -> list[tuple[float, Path]]:
    """
    Extract one frame every `interval` seconds using FFmpeg's fps filter.
    Returns list of (timestamp_seconds, temp_frame_path) pairs.
    """
    out_pattern = str(temp_dir / "uniform_%06d.jpg")
    cmd = [
        "ffmpeg", "-i", str(video_path),
        "-vf", f"fps=1/{interval}",
        "-q:v", "2",
        out_pattern,
        "-y",
    ]
    subprocess.run(cmd, capture_output=True)
    frame_files = sorted(temp_dir.glob("uniform_*.jpg"))
    # fps=1/N gives frames at t=0, N, 2N, ...
    return [(float(i * interval), f) for i, f in enumerate(frame_files)]


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------

def filter_by_resolution(
    frames: list[tuple[float, Path]], min_w: int, min_h: int
) -> tuple[list[tuple[float, Path]], int]:
    """Discard frames below the minimum resolution. Returns (kept, rejected_count)."""
    kept, rejected = [], 0
    for ts, path in frames:
        with Image.open(path) as img:
            w, h = img.size
        if w >= min_w and h >= min_h:
            kept.append((ts, path))
        else:
            rejected += 1
            path.unlink(missing_ok=True)
    return kept, rejected


def dedup_frames(
    frames: list[tuple[float, Path]], threshold_pct: int
) -> tuple[list[tuple[float, Path]], int]:
    """
    Remove perceptually similar frames using imagehash (phash, 64 bits).
    Keeps the first of any near-duplicate group.
    threshold_pct=90 means discard if ≥90% similar (hamming distance ≤ 6).
    Returns (kept, removed_count).
    """
    max_dist = int((1 - threshold_pct / 100) * 64)
    kept: list[tuple[float, Path]] = []
    kept_hashes: list[imagehash.ImageHash] = []
    removed = 0

    for ts, path in frames:
        with Image.open(path) as img:
            h = imagehash.phash(img)
        if any(h - kh <= max_dist for kh in kept_hashes):
            removed += 1
            path.unlink(missing_ok=True)
        else:
            kept.append((ts, path))
            kept_hashes.append(h)

    return kept, removed


# ---------------------------------------------------------------------------
# Per-video processing
# ---------------------------------------------------------------------------

def process_video(
    conn: sqlite3.Connection, video: dict, cfg: dict
) -> tuple[int, int, int]:
    """
    Extract, filter, and record frames for a single video.
    Returns (frames_extracted, res_rejected, dedup_removed).
    """
    video_id = video["video_id"]
    market = slugify(video["market"]) if video["market"] else "unknown"
    creator = slugify(video["creator_name"]) if video["creator_name"] else "unknown"

    video_path = find_video_file(video_id)
    if not video_path:
        log.warning("  No video file found for %s — skipping", video_id)
        return 0, 0, 0

    min_w = cfg.get("min_resolution_width", 1920)
    min_h = cfg.get("min_resolution_height", 1080)
    min_frames = cfg.get("min_frames_per_video", 10)
    interval = cfg.get("uniform_sampling_interval", 30)
    dedup_threshold = cfg.get("dedup_threshold", 90)
    method = cfg.get("frame_extraction_method", "scene_change")

    out_dir = FRAMES_DIR / market / creator
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="siftr_") as tmp:
        temp_dir = Path(tmp)

        # --- Extraction ---
        if method == "uniform":
            log.info("  Uniform sampling every %ds", interval)
            frames = extract_uniform_frames(video_path, temp_dir, interval)
            log.info("  %d frame(s) extracted", len(frames))
        else:
            log.info("  Scene-change extraction (threshold %.1f)", SCENE_THRESHOLD)
            frames = extract_scene_frames(video_path, temp_dir)
            log.info("  %d scene-change frame(s) found", len(frames))

            if len(frames) < min_frames:
                log.info(
                    "  Too few (%d < %d minimum) — falling back to uniform every %ds",
                    len(frames), min_frames, interval,
                )
                for _, p in frames:
                    p.unlink(missing_ok=True)
                frames = extract_uniform_frames(video_path, temp_dir, interval)
                log.info("  Uniform fallback: %d frame(s)", len(frames))

        # --- Resolution filter ---
        frames, res_rejected = filter_by_resolution(frames, min_w, min_h)
        log.info(
            "  Resolution filter: %d rejected (below %dx%d), %d remaining",
            res_rejected, min_w, min_h, len(frames),
        )

        # --- Deduplication ---
        frames, dedup_removed = dedup_frames(frames, dedup_threshold)
        log.info("  Dedup: %d removed, %d remaining", dedup_removed, len(frames))

        # --- Save and record ---
        extraction_date = datetime.now(timezone.utc).isoformat()
        extracted = 0
        for ts, temp_path in frames:
            timecode = seconds_to_timecode(ts)
            ts_str = seconds_to_filename_ts(ts)
            stem = f"{market}__{creator}__{video_id}__{ts_str}"
            dest = out_dir / f"{stem}.jpg"
            shutil.copy2(temp_path, dest)

            with Image.open(dest) as img:
                w, h = img.size

            conn.execute(
                """
                INSERT OR IGNORE INTO frames
                  (frame_id, video_id, timestamp_seconds, timecode,
                   resolution, status, file_path, extraction_date)
                VALUES (?, ?, ?, ?, ?, 'unreviewed', ?, ?)
                """,
                (
                    stem,
                    video_id,
                    round(ts, 3),
                    timecode,
                    f"{w}x{h}",
                    str(dest.relative_to(BASE_DIR)),
                    extraction_date,
                ),
            )
            extracted += 1

        conn.commit()

    return extracted, res_rejected, dedup_removed


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_extraction() -> dict:
    """
    Extract frames for all videos not yet processed.
    Idempotent: videos that already have frames in the DB are skipped.
    Returns summary counts.
    """
    check_ffmpeg()
    cfg = load_config()
    conn = sqlite3.connect(DB_PATH)

    rows = conn.execute("""
        SELECT v.video_id, v.creator_name, v.market, v.video_title
        FROM videos v
        WHERE NOT EXISTS (
            SELECT 1 FROM frames f WHERE f.video_id = v.video_id
        )
    """).fetchall()
    videos = [
        {"video_id": r[0], "creator_name": r[1], "market": r[2], "title": r[3]}
        for r in rows
    ]

    log.info("=" * 60)
    log.info("SIFTR frame extraction started")
    log.info("%d video(s) need frame extraction", len(videos))

    total_extracted = total_res_rej = total_dedup_rem = 0

    for i, video in enumerate(videos, start=1):
        log.info(
            "[%d/%d] %s — %s",
            i, len(videos), video["video_id"], video["title"][:60],
        )
        extracted, res_rej, dedup_rem = process_video(conn, video, cfg)
        total_extracted += extracted
        total_res_rej += res_rej
        total_dedup_rem += dedup_rem

    total_in_db = conn.execute("SELECT COUNT(*) FROM frames").fetchone()[0]
    conn.close()

    log.info("-" * 60)
    log.info("Frame extraction summary:")
    log.info("  Frames extracted this run  : %d", total_extracted)
    log.info("  Rejected by resolution     : %d", total_res_rej)
    log.info("  Removed by deduplication   : %d", total_dedup_rem)
    log.info("  Total frames in database   : %d", total_in_db)
    log.info("=" * 60)

    return {
        "extracted": total_extracted,
        "res_rejected": total_res_rej,
        "dedup_removed": total_dedup_rem,
        "total_in_db": total_in_db,
    }


if __name__ == "__main__":
    run_extraction()

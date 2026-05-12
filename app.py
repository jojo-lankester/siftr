from __future__ import annotations

import csv
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import yaml
from datetime import datetime, timezone, timedelta

from flask import Flask, abort, jsonify, render_template, request, send_file, url_for

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "database.sqlite")
FRAMES_DIR = os.path.join(BASE_DIR, "frames")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")
AVATARS_DIR = os.path.join(BASE_DIR, "creator_avatars")

YAML_PATH   = os.path.join(BASE_DIR, "videos.yaml")
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

KNOWN_MARKETS = ["AU", "BR", "DE", "EU", "FR", "ID", "IN", "JP", "UK", "US"]

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

_export_jobs: dict[str, dict] = {}
_export_jobs_lock = threading.Lock()

_yaml_lock = threading.Lock()  # serialise all videos.yaml reads+writes

app = Flask(__name__)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _update_job(job_id: str, **kwargs) -> None:
    with _jobs_lock:
        if job_id in _jobs:
            _jobs[job_id].update(kwargs)


def _read_log_tail(n: int = 25) -> str:
    log_path = os.path.join(BASE_DIR, "logs", "harvest.log")
    try:
        with open(log_path, encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-n:]).strip()
    except Exception:
        return ""


def _update_export_job(job_id: str, **kwargs) -> None:
    with _export_jobs_lock:
        if job_id in _export_jobs:
            _export_jobs[job_id].update(kwargs)


def _timecode_to_seconds(timecode: str) -> int:
    """Convert HH:MM:SS.mmm timecode string to integer seconds."""
    parts = timecode.split(":")
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return int(h * 3600 + m * 60 + s)


def _log_capture(frame_id: str, event: str, error: str = "") -> None:
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts}  {event.upper():8}  {frame_id}"
    if error:
        line += f"  — {error}"
    with open(os.path.join(log_dir, "capture.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _run_export_job(job_id: str, market_code: str, frames: list[dict],
                    export_round: int, export_dir: str, folder_name: str,
                    avatar_map: dict[str, str | None] | None = None) -> None:
    import playwright_capture as pc

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    timeout_ms = int(cfg.get("playwright_timeout_seconds", 60)) * 1000
    delay     = float(cfg.get("playwright_delay_between_captures", 2))
    headless  = bool(cfg.get("playwright_headless", True))

    conn = get_db()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest_rows: list[dict] = []
    results: dict[str, dict] = {}

    for i, frame in enumerate(frames):
        with _export_jobs_lock:
            if _export_jobs[job_id].get("cancelled", False):
                break

        frame_id  = frame["frame_id"]
        parts     = frame_id.split("__")
        video_url = frame["video_url"] or f"https://www.youtube.com/watch?v={parts[2]}"
        timestamp = _timecode_to_seconds(frame["timecode"])
        output_path = os.path.join(export_dir, frame_id + ".jpg")

        _update_export_job(job_id,
            current_frame_id=frame_id,
            current_frame_timecode=frame["timecode"])
        _log_capture(frame_id, "attempt")

        try:
            pc.capture_frame(video_url, timestamp, output_path,
                             headless=headless, timeout_ms=timeout_ms)

            first_exported_at = frame["exported_at"] or now
            conn.execute("""
                UPDATE frames SET
                    export_status = 'exported',
                    exported_at   = ?,
                    export_round  = ?,
                    export_error  = NULL
                WHERE frame_id = ?
            """, (now, export_round, frame_id))
            conn.commit()

            manifest_rows.append({
                "frame_id":          frame_id,
                "creator_name":      frame["creator_name"],
                "market":            frame["market"],
                "video_title":       frame["video_title"],
                "video_url":         video_url,
                "timecode":          frame["timecode"],
                "themes":            frame["themes"] or "",
                "export_round":      export_round,
                "first_exported_at": first_exported_at,
                "this_exported_at":  now,
                "resolution":        "1920x1080",
                "high_res_status":   "success",
                "avatar_filename":   "",  # filled in below after avatar copy
            })
            results[frame_id] = {"export_status": "exported"}
            _log_capture(frame_id, "success")

            with _export_jobs_lock:
                _export_jobs[job_id]["exported"] += 1
                _export_jobs[job_id]["current"]   = i + 1

        except Exception as exc:
            error_msg = str(exc)
            if os.path.exists(output_path):
                os.remove(output_path)

            conn.execute("""
                UPDATE frames SET
                    export_status = 'export_failed',
                    export_error  = ?
                WHERE frame_id = ?
            """, (error_msg, frame_id))
            conn.commit()

            results[frame_id] = {"export_status": "export_failed", "error": error_msg}
            _log_capture(frame_id, "failed", error_msg)

            with _export_jobs_lock:
                _export_jobs[job_id]["failed"]  += 1
                _export_jobs[job_id]["current"]  = i + 1

        # Polite delay between captures
        with _export_jobs_lock:
            cancelled = _export_jobs[job_id].get("cancelled", False)
        if not cancelled and i < len(frames) - 1:
            time.sleep(delay)

    # Copy creator avatars into export_dir/avatars/
    slug_to_avatar_filename: dict[str, str] = {}
    if avatar_map:
        avatars_export_dir = os.path.join(export_dir, "avatars")
        os.makedirs(avatars_export_dir, exist_ok=True)
        for frame in frames:
            slug = frame.get("creator_slug", "")
            if slug in slug_to_avatar_filename:
                continue
            rel = (avatar_map or {}).get(slug)
            if rel:
                src = os.path.join(BASE_DIR, rel)
                if os.path.isfile(src):
                    dst_name = f"{slug}.jpg"
                    try:
                        shutil.copy2(src, os.path.join(avatars_export_dir, dst_name))
                        slug_to_avatar_filename[slug] = f"avatars/{dst_name}"
                    except Exception:
                        pass
            if slug not in slug_to_avatar_filename:
                slug_to_avatar_filename[slug] = ""

    # Backfill avatar_filename into manifest rows
    for row in manifest_rows:
        slug = next(
            (f.get("creator_slug", "") for f in frames if f["frame_id"] == row["frame_id"]),
            "",
        )
        row["avatar_filename"] = slug_to_avatar_filename.get(slug, "")

    # Write manifest (successful frames only)
    csv_path = os.path.join(export_dir, "manifest.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(manifest_rows)

    conn.close()

    with _export_jobs_lock:
        was_cancelled = _export_jobs[job_id].get("cancelled", False)
        _export_jobs[job_id].update({
            "status":               "cancelled" if was_cancelled else "complete",
            "results":              results,
            "current_frame_id":     None,
            "current_frame_timecode": None,
        })


def discover_channel_videos(channel_url: str, max_videos: int = 3, months: int = 18) -> list[dict]:
    """Use yt-dlp to fetch the top N most-viewed videos from a channel in the last 18 months."""
    import yt_dlp
    from datetime import datetime, timedelta

    cutoff = datetime.now() - timedelta(days=int(months * 30.44))
    ydl_opts = {
        "extract_flat": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "playlistend": 60,
        "dateafter": cutoff.strftime("%Y%m%d"),
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(channel_url, download=False)

    if not info or "entries" not in info:
        raise ValueError("Could not find any videos for this channel. Check the URL and try again.")

    entries = [e for e in (info.get("entries") or []) if e and e.get("id")]
    if not entries:
        raise ValueError("No videos found in the last 18 months for this channel.")

    entries.sort(key=lambda e: e.get("view_count") or 0, reverse=True)
    top = entries[:max_videos]

    return [
        {
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "title": e.get("title", e["id"]),
            "view_count": e.get("view_count"),
        }
        for e in top
    ]


def update_videos_yaml(market_code: str, creator_name: str, creator_slug: str,
                        themes: list[str], video_urls: list[str],
                        avatar_path: str | None = None) -> None:
    """Append a new creator+videos under market_code in videos.yaml. Non-destructive."""
    with _yaml_lock:
        with open(YAML_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        markets = data.setdefault("markets", [])
        market_entry = next((m for m in markets if m.get("code") == market_code), None)
        if not market_entry:
            market_entry = {"code": market_code, "creators": []}
            markets.append(market_entry)

        market_entry.setdefault("creators", []).append({
            "name": creator_name,
            "creator_slug": creator_slug,
            "avatar_path": avatar_path,
            "default_themes": themes,
            "videos": [{"url": u} for u in video_urls],
        })

        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


def _remove_video_from_yaml(video_url: str) -> None:
    """Remove a single video URL from videos.yaml (no-op if URL not found)."""
    with _yaml_lock:
        with open(YAML_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        changed = False
        for market in data.get("markets", []):
            for creator in market.get("creators", []):
                videos = creator.get("videos", [])
                filtered = [v for v in videos if v.get("url") != video_url]
                if len(filtered) < len(videos):
                    creator["videos"] = filtered
                    changed = True

        if changed:
            with open(YAML_PATH, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False,
                          sort_keys=False, allow_unicode=True)


def append_videos_to_creator(market_code: str, creator_slug: str,
                              new_urls: list[str]) -> tuple[int, str]:
    """
    Append new video URLs to an existing creator (idempotent — skips duplicates).
    Returns (added_count, creator_name).  Raises ValueError if creator not found.
    """
    with _yaml_lock:
        with open(YAML_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        creator_name: str | None = None
        added = 0
        for m in data.get("markets", []):
            if m.get("code") != market_code:
                continue
            for c in m.get("creators", []):
                if c.get("creator_slug") != creator_slug:
                    continue
                creator_name = c.get("name", creator_slug)
                existing_urls = {v["url"] for v in c.get("videos", [])}
                to_add = [u for u in new_urls if u not in existing_urls]
                added = len(to_add)
                c.setdefault("videos", []).extend({"url": u} for u in to_add)
                break
            if creator_name is not None:
                break

        if creator_name is None:
            raise ValueError(
                f"Creator '{creator_slug}' not found in market {market_code}."
            )

        if added > 0:
            with open(YAML_PATH, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False,
                          sort_keys=False, allow_unicode=True)

    return added, creator_name


def _run_add_creator_job(job_id: str, market_code: str, creator_name: str,
                          creator_slug: str, themes: list[str],
                          input_method: str, channel_url: str, video_urls_raw: str) -> None:
    try:
        video_urls: list[str] = []
        discovered: list[dict] = []

        if input_method == "channel":
            _update_job(job_id, phase="discovering", message="Discovering videos from channel…")
            discovered = discover_channel_videos(channel_url)
            video_urls = [v["url"] for v in discovered]
            _update_job(job_id, discovered_videos=discovered)
        else:
            video_urls = [u.strip() for u in video_urls_raw.splitlines() if u.strip()]

        if not video_urls:
            _update_job(job_id, status="failed", error="No valid video URLs found.")
            return

        _update_job(job_id, phase="updating_yaml", message="Updating configuration…")
        update_videos_yaml(market_code, creator_name, creator_slug, themes, video_urls)

        _update_job(job_id, phase="harvesting",
                    message=f"Harvesting {len(video_urls)} video(s) — this may take a few minutes…",
                    video_count=len(video_urls))

        subprocess.run(
            [sys.executable, "harvest.py"],
            capture_output=True, text=True,
            cwd=BASE_DIR, timeout=3600,
        )

        # Fetch creator avatar (best-effort — never fails the job)
        _update_job(job_id, phase="fetching_avatar",
                    message="Fetching creator avatar…")
        avatar_source = channel_url if (input_method == "channel" and channel_url) \
                        else (video_urls[0] if video_urls else None)
        avatar_path = None
        if avatar_source:
            avatar_path = _fetch_and_save_avatar(avatar_source, market_code, creator_slug)
        _update_creator_avatar_in_yaml(market_code, creator_slug, avatar_path)

        _update_job(job_id,
                    status="complete", phase="complete",
                    message="Done",
                    log_tail=_read_log_tail(),
                    video_count=len(video_urls))

    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


def _run_add_videos_job(job_id: str, market_code: str,
                         creator_slug: str, video_urls_raw: str) -> None:
    try:
        video_urls = [u.strip() for u in video_urls_raw.splitlines() if u.strip()]
        if not video_urls:
            _update_job(job_id, status="failed", error="No valid video URLs found.")
            return

        _update_job(job_id, phase="updating_yaml", message="Updating configuration…")
        added_count, creator_name = append_videos_to_creator(
            market_code, creator_slug, video_urls
        )
        _update_job(job_id, creator_name=creator_name, video_count=added_count)

        if added_count == 0:
            _update_job(job_id,
                        status="complete", phase="complete",
                        message="Done — all URLs already present.",
                        video_count=0)
            return

        _update_job(job_id, phase="harvesting",
                    message=f"Harvesting {added_count} new video(s) — this may take a few minutes…")

        subprocess.run(
            [sys.executable, "harvest.py"],
            capture_output=True, text=True,
            cwd=BASE_DIR, timeout=3600,
        )

        _update_job(job_id,
                    status="complete", phase="complete",
                    message="Done",
                    log_tail=_read_log_tail(),
                    video_count=added_count)

    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


_NEW_THRESHOLD_HOURS = 24


def _is_new(date_added: str | None) -> bool:
    if not date_added:
        return False
    try:
        dt = datetime.fromisoformat(date_added)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt) < timedelta(hours=_NEW_THRESHOLD_HOURS)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Creator avatar helpers
# ---------------------------------------------------------------------------

def _creator_initials(name: str) -> str:
    words = name.split()
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    return name[:2].upper()


def _fetch_and_save_avatar(source_url: str, market_code: str,
                            creator_slug: str) -> str | None:
    """
    Fetch the highest-res channel avatar and save to creator_avatars/.
    source_url may be a channel URL or a video URL.
    Returns relative path (e.g. 'creator_avatars/au/brandon_b.jpg') or None.
    """
    import urllib.request
    import yt_dlp

    out_dir = os.path.join(AVATARS_DIR, market_code.lower())
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{creator_slug}.jpg")

    try:
        quiet = {"quiet": True, "no_warnings": True, "skip_download": True}

        with yt_dlp.YoutubeDL({**quiet, "extract_flat": True}) as ydl:
            info = ydl.extract_info(source_url, download=False)

        if not info:
            return None

        # If source was a video URL, entries won't be present — re-fetch channel page
        if "entries" not in info:
            ch_url = info.get("channel_url") or info.get("uploader_url")
            if ch_url and ch_url != source_url:
                with yt_dlp.YoutubeDL({**quiet, "extract_flat": True}) as ydl:
                    ch_info = ydl.extract_info(ch_url, download=False)
                if ch_info:
                    info = ch_info

        thumbnails = info.get("thumbnails") or []
        if not thumbnails:
            print(f"SIFTR: No channel thumbnails for {creator_slug}", flush=True)
            return None

        # Highest resolution — channel avatars are typically square (800×800 max)
        best = max(thumbnails,
                   key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
        img_url = best.get("url")
        if not img_url:
            return None

        req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            img_data = resp.read()

        if len(img_data) < 500:
            return None

        with open(out_path, "wb") as f:
            f.write(img_data)

        return f"creator_avatars/{market_code.lower()}/{creator_slug}.jpg"

    except Exception as exc:
        print(f"SIFTR: Avatar fetch warning for {creator_slug}: {exc}", flush=True)
        return None


def _update_creator_avatar_in_yaml(market_code: str, creator_slug: str,
                                    avatar_path: str | None) -> None:
    with _yaml_lock:
        with open(YAML_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for market in data.get("markets", []):
            if market.get("code") == market_code:
                for creator in market.get("creators", []):
                    if creator.get("creator_slug") == creator_slug:
                        creator["avatar_path"] = avatar_path
                        break
        with open(YAML_PATH, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False,
                      sort_keys=False, allow_unicode=True)


def _merge_yaml_duplicates() -> None:
    """
    Merge creators that share the same slug within the same market.
    Runs synchronously at startup before any request is handled.
    """
    with _yaml_lock:
        try:
            with open(YAML_PATH, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            return

        changed = False
        for market in data.get("markets", []):
            seen: dict[str, dict] = {}
            merged_creators: list[dict] = []
            for c in market.get("creators", []):
                slug = c.get("creator_slug", "")
                if slug and slug in seen:
                    primary = seen[slug]
                    if not primary.get("avatar_path") and c.get("avatar_path"):
                        primary["avatar_path"] = c["avatar_path"]
                    if not primary.get("default_themes") and c.get("default_themes"):
                        primary["default_themes"] = c["default_themes"]
                    existing_urls = {v["url"] for v in primary.get("videos", [])}
                    to_add = [v for v in c.get("videos", []) if v.get("url") not in existing_urls]
                    primary.setdefault("videos", []).extend(to_add)
                    print(
                        f"SIFTR: Merged duplicate '{slug}' in market "
                        f"'{market.get('code')}' — added {len(to_add)} URL(s)",
                        flush=True,
                    )
                    changed = True
                else:
                    seen[slug] = c
                    merged_creators.append(c)
            market["creators"] = merged_creators

        if changed:
            with open(YAML_PATH, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False,
                          sort_keys=False, allow_unicode=True)
            print("SIFTR: videos.yaml rewritten after duplicate merge.", flush=True)


def _backfill_avatars() -> None:
    """Background startup task: fetch missing avatars for existing creators."""
    try:
        with open(YAML_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        to_fetch = [
            {
                "market_code": m["code"],
                "creator_slug": c["creator_slug"],
                "source_url": (c.get("videos") or [{}])[0].get("url", ""),
            }
            for m in data.get("markets", [])
            for c in m.get("creators", [])
            if not c.get("avatar_path") and (c.get("videos") or [])
        ]

        if not to_fetch:
            return

        print(f"SIFTR: Backfilling avatars for {len(to_fetch)} creator(s)…", flush=True)
        for item in to_fetch:
            if not item["source_url"]:
                continue
            print(f"SIFTR: Fetching avatar — {item['creator_slug']}", flush=True)
            avatar_path = _fetch_and_save_avatar(
                item["source_url"], item["market_code"], item["creator_slug"]
            )
            _update_creator_avatar_in_yaml(
                item["market_code"], item["creator_slug"], avatar_path
            )
            status = avatar_path or "failed"
            print(f"SIFTR: Avatar backfill {item['creator_slug']}: {status}", flush=True)

    except Exception as exc:
        print(f"SIFTR: Avatar backfill error: {exc}", flush=True)


# ---------------------------------------------------------------------------
# DB migration — runs on startup, idempotent
# ---------------------------------------------------------------------------

def migrate_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    existing = {row[1] for row in cur.execute("PRAGMA table_info(frames)").fetchall()}

    if "export_status" not in existing:
        cur.execute("ALTER TABLE frames ADD COLUMN export_status TEXT DEFAULT 'unreviewed'")
        # Migrate existing status values (unreviewed / shortlisted)
        cur.execute("UPDATE frames SET export_status = status")

    if "exported_at" not in existing:
        cur.execute("ALTER TABLE frames ADD COLUMN exported_at TEXT")

    if "export_round" not in existing:
        cur.execute("ALTER TABLE frames ADD COLUMN export_round INTEGER")

    if "export_error" not in existing:
        cur.execute("ALTER TABLE frames ADD COLUMN export_error TEXT")

    conn.commit()
    conn.close()


migrate_db()
_merge_yaml_duplicates()
threading.Thread(target=_backfill_avatars, daemon=True).start()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Market chooser
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    conn = get_db()
    rows = conn.execute("""
        SELECT
            v.market,
            COUNT(DISTINCT v.creator_name)                                       AS creator_count,
            COUNT(DISTINCT v.video_id)                                           AS video_count,
            COUNT(f.frame_id)                                                    AS frame_count,
            SUM(CASE WHEN f.export_status = 'unreviewed'    THEN 1 ELSE 0 END)  AS unreviewed_count,
            SUM(CASE WHEN f.export_status = 'shortlisted'   THEN 1 ELSE 0 END)  AS shortlisted_count,
            SUM(CASE WHEN f.export_status = 'exported'      THEN 1 ELSE 0 END)  AS exported_count,
            SUM(CASE WHEN f.export_status = 'export_failed' THEN 1 ELSE 0 END)  AS failed_count
        FROM videos v
        LEFT JOIN frames f ON v.video_id = f.video_id
        GROUP BY v.market
        ORDER BY v.market
    """).fetchall()
    conn.close()
    markets = [dict(r) for r in rows]
    return render_template("market_chooser.html", markets=markets)


# ---------------------------------------------------------------------------
# Main review grid
# ---------------------------------------------------------------------------

# Maps show= param to SQL WHERE clause on the frames table (no alias)
SHOW_CLAUSES = {
    "unreviewed":  "AND export_status = 'unreviewed'",
    "shortlisted": "AND export_status = 'shortlisted'",
    "exported":    "AND export_status = 'exported'",
    "failed":      "AND export_status = 'export_failed'",
}


@app.route("/market/<market_code>")
def review(market_code: str):
    conn = get_db()

    exists = conn.execute(
        "SELECT 1 FROM videos WHERE market = ? LIMIT 1", (market_code,)
    ).fetchone()
    if not exists:
        conn.close()
        abort(404)

    show = request.args.get("show", "")
    creator_filter = request.args.get("creator", "")
    theme_filter = request.args.get("theme", "")
    video_id_filter = request.args.get("video_id", "")

    # Market-level stats
    stats = conn.execute("""
        SELECT
            COUNT(DISTINCT v.video_id)                                           AS video_count,
            COUNT(f.frame_id)                                                    AS frame_count,
            SUM(CASE WHEN f.export_status = 'unreviewed'    THEN 1 ELSE 0 END)  AS unreviewed_count,
            SUM(CASE WHEN f.export_status = 'shortlisted'   THEN 1 ELSE 0 END)  AS shortlisted_count,
            SUM(CASE WHEN f.export_status = 'exported'      THEN 1 ELSE 0 END)  AS exported_count,
            SUM(CASE WHEN f.export_status = 'export_failed' THEN 1 ELSE 0 END)  AS failed_count
        FROM videos v
        LEFT JOIN frames f ON v.video_id = f.video_id
        WHERE v.market = ?
    """, (market_code,)).fetchone()

    if not show:
        show = "all"

    creators = [r["creator_name"] for r in conn.execute(
        "SELECT DISTINCT creator_name FROM videos WHERE market = ? ORDER BY creator_name",
        (market_code,)
    ).fetchall()]

    all_theme_rows = conn.execute(
        "SELECT DISTINCT themes FROM videos WHERE market = ? AND themes != ''",
        (market_code,)
    ).fetchall()
    all_themes: set[str] = set()
    for row in all_theme_rows:
        all_themes.update(t.strip() for t in (row["themes"] or "").split(",") if t.strip())
    all_themes_list = sorted(all_themes)

    # Build creator_slug → avatar_path map from videos.yaml
    with open(YAML_PATH, encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f) or {}
    avatar_map: dict[str, str | None] = {
        c.get("creator_slug", ""): c.get("avatar_path")
        for m in yaml_data.get("markets", [])
        for c in m.get("creators", [])
    }

    status_clause = SHOW_CLAUSES.get(show, "")
    creator_clause = "AND v.creator_name = ?" if creator_filter else ""
    creator_params: list = [creator_filter] if creator_filter else []
    video_id_clause = "AND v.video_id = ?" if video_id_filter else ""
    video_id_params: list = [video_id_filter] if video_id_filter else []

    video_rows = conn.execute(f"""
        SELECT
            v.video_id, v.video_title, v.creator_name, v.creator_slug,
            v.themes, v.market, v.date_added,
            COUNT(f.frame_id)                                                    AS total_frames,
            SUM(CASE WHEN f.export_status = 'unreviewed'    THEN 1 ELSE 0 END)  AS unreviewed,
            SUM(CASE WHEN f.export_status = 'shortlisted'   THEN 1 ELSE 0 END)  AS shortlisted,
            SUM(CASE WHEN f.export_status = 'exported'      THEN 1 ELSE 0 END)  AS exported,
            SUM(CASE WHEN f.export_status = 'export_failed' THEN 1 ELSE 0 END)  AS failed
        FROM videos v
        LEFT JOIN frames f ON v.video_id = f.video_id
        WHERE v.market = ?
        {creator_clause}
        {video_id_clause}
        GROUP BY v.video_id
    """, [market_code] + creator_params + video_id_params).fetchall()

    # Collect videos into per-creator buckets (maintaining insertion order for
    # later sort); also track each creator's min date_added for group ordering.
    by_creator: dict[str, dict] = {}

    for vrow in video_rows:
        video_id = vrow["video_id"]
        video_themes = [t.strip() for t in (vrow["themes"] or "").split(",") if t.strip()]

        if theme_filter and theme_filter not in video_themes:
            continue

        frames = conn.execute(f"""
            SELECT frame_id, timestamp_seconds, timecode, export_status, file_path
            FROM frames
            WHERE video_id = ? {status_clause}
            ORDER BY timestamp_seconds
        """, (video_id,)).fetchall()

        if not frames and show != "all":
            continue

        date_added = vrow["date_added"] or ""
        video = {
            "video_id": video_id,
            "video_title": vrow["video_title"],
            "creator_name": vrow["creator_name"],
            "creator_slug": vrow["creator_slug"],
            "themes": video_themes,
            "total_frames": vrow["total_frames"],
            "unreviewed": vrow["unreviewed"] or 0,
            "shortlisted": vrow["shortlisted"] or 0,
            "exported": vrow["exported"] or 0,
            "failed": vrow["failed"] or 0,
            "frames": [dict(f) for f in frames],
            "date_added": date_added,
            "is_new": _is_new(date_added),
        }

        cn = vrow["creator_name"]
        if cn not in by_creator:
            by_creator[cn] = {
                "creator_name": cn,
                "creator_slug": vrow["creator_slug"],
                "videos": [],
                "min_date": date_added,
            }
        else:
            if date_added and (not by_creator[cn]["min_date"] or date_added < by_creator[cn]["min_date"]):
                by_creator[cn]["min_date"] = date_added
        by_creator[cn]["videos"].append(video)

    # Sort creators by min date_added DESC (most recently added creator first),
    # and videos within each creator by date_added DESC.
    groups: list[dict] = []
    for creator_data in sorted(by_creator.values(), key=lambda c: c["min_date"], reverse=True):
        videos = sorted(creator_data["videos"], key=lambda v: v["date_added"], reverse=True)
        slug = creator_data["creator_slug"]
        groups.append({
            "creator_name":     creator_data["creator_name"],
            "creator_slug":     slug,
            "avatar_path":      avatar_map.get(slug),
            "avatar_initials":  _creator_initials(creator_data["creator_name"]),
            "videos":           videos,
            "is_new":           any(v["is_new"] for v in videos),
            "total_shortlisted": sum(v["shortlisted"] for v in videos),
            "total_exported":   sum(v["exported"] for v in videos),
            "total_failed":     sum(v["failed"] for v in videos),
        })

    conn.close()
    return render_template(
        "review.html",
        market_code=market_code,
        stats=dict(stats),
        groups=groups,
        show=show,
        creators=creators,
        creator_filter=creator_filter,
        all_themes=all_themes_list,
        theme_filter=theme_filter,
        video_id_filter=video_id_filter,
    )


# ---------------------------------------------------------------------------
# Serve frame images
# ---------------------------------------------------------------------------

@app.route("/frames/<market>/<creator_slug>/<filename>")
def serve_frame(market: str, creator_slug: str, filename: str):
    path = os.path.join(FRAMES_DIR, market, creator_slug, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@app.route("/creator_avatars/<market>/<filename>")
def serve_avatar(market: str, filename: str):
    path = os.path.join(AVATARS_DIR, market, filename)
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


# ---------------------------------------------------------------------------
# API: toggle export_status
#
# Transitions:
#   unreviewed    → shortlisted       (designer selects)
#   shortlisted   → unreviewed        (designer deselects)
#   export_failed → shortlisted       (re-queue for retry)
#   exported      → exported          (no-op; managed by export process)
# ---------------------------------------------------------------------------

_TOGGLE: dict[str, str] = {
    "unreviewed":    "shortlisted",
    "shortlisted":   "unreviewed",
    "export_failed": "shortlisted",
    "exported":      "exported",
}


@app.get("/api/frame/<frame_id>/nearby")
def get_nearby_frames(frame_id: str):
    conn = get_db()
    frame = conn.execute(
        "SELECT video_id, timestamp_seconds, timecode FROM frames WHERE frame_id = ?",
        (frame_id,),
    ).fetchone()
    if not frame:
        conn.close()
        return jsonify({"error": "not found"}), 404

    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    window = int(cfg.get("nearby_frames_window", 60))

    rows = conn.execute("""
        SELECT frame_id FROM frames
        WHERE video_id = ?
          AND ABS(timestamp_seconds - ?) <= ?
        ORDER BY timestamp_seconds
    """, (frame["video_id"], frame["timestamp_seconds"], window)).fetchall()
    conn.close()

    return jsonify({
        "frame_id":         frame_id,
        "timecode":         frame["timecode"],
        "nearby_frame_ids": [r["frame_id"] for r in rows],
        "window_seconds":   window,
    })


_NEARBY_EXTRACT_WINDOW = 5   # ±5 seconds
_NEARBY_EXTRACT_FPS    = 2   # 0.5-second intervals
_NEARBY_DENSITY_THRESH = 5   # if >5 frames exist in the window, skip re-extraction


@app.post("/api/frame/<frame_id>/extract_nearby")
def extract_nearby_frames(frame_id: str):
    """On-demand fine-grained frame extraction around a timecode."""
    conn = get_db()
    frame = conn.execute("""
        SELECT f.frame_id, f.video_id, f.timestamp_seconds, f.timecode,
               v.market, v.creator_slug
        FROM frames f
        JOIN videos v ON f.video_id = v.video_id
        WHERE f.frame_id = ?
    """, (frame_id,)).fetchone()
    if not frame:
        conn.close()
        return jsonify({"error": "not found"}), 404

    video_id     = frame["video_id"]
    ts           = float(frame["timestamp_seconds"])
    market       = frame["market"].lower()
    creator_slug = frame["creator_slug"]
    W            = _NEARBY_EXTRACT_WINDOW

    # Idempotency: if we already have a dense cluster, return existing frames
    existing = conn.execute("""
        SELECT frame_id, timestamp_seconds, timecode, export_status
        FROM frames
        WHERE video_id = ? AND ABS(timestamp_seconds - ?) <= ?
        ORDER BY timestamp_seconds
    """, (video_id, ts, W)).fetchall()

    if len(existing) > _NEARBY_DENSITY_THRESH:
        def _to_dict(r):
            parts = r["frame_id"].split("__")
            return {
                "frame_id":          r["frame_id"],
                "timecode":          r["timecode"],
                "timestamp_seconds": r["timestamp_seconds"],
                "export_status":     r["export_status"],
                "img_url":           f"/frames/{parts[0]}/{parts[1]}/{r['frame_id']}.jpg",
            }
        conn.close()
        return jsonify({"status": "already_extracted", "frames": [_to_dict(r) for r in existing]})

    # Find the source video file
    raw_dir    = os.path.join(FRAMES_DIR, "_raw_downloads")
    video_path = None
    for ext in ("mp4", "webm", "mkv"):
        p = os.path.join(raw_dir, f"{video_id}.{ext}")
        if os.path.isfile(p):
            video_path = p
            break

    if not video_path:
        conn.close()
        return jsonify({"error": "source_video_missing"}), 404

    # Extract frames with FFmpeg
    start_ts = max(0.0, ts - W)
    duration  = W * 2   # 10 seconds total

    out_dir = os.path.join(FRAMES_DIR, market, creator_slug)
    os.makedirs(out_dir, exist_ok=True)

    result_frames: list[dict] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start_ts),
                "-t",  str(duration),
                "-i",  video_path,
                "-vf", f"fps={_NEARBY_EXTRACT_FPS}",
                "-q:v", "5",
                os.path.join(tmpdir, "frame_%04d.jpg"),
            ],
            capture_output=True,
            timeout=90,
        )

        if proc.returncode != 0:
            conn.close()
            err = proc.stderr.decode(errors="replace")[-300:]
            return jsonify({"error": f"ffmpeg_failed: {err}"}), 500

        for i, img_path in enumerate(
            sorted(glob.glob(os.path.join(tmpdir, "frame_*.jpg")))
        ):
            frame_ts_f = round(start_ts + i / _NEARBY_EXTRACT_FPS, 3)
            total_s    = int(frame_ts_f)
            ms         = round((frame_ts_f - total_s) * 1000)
            h          = total_s // 3600
            m          = (total_s % 3600) // 60
            s          = total_s % 60

            tc_file      = f"{h:02d}-{m:02d}-{s:02d}.{ms:03d}"
            tc_display   = f"{h:02d}:{m:02d}:{s:02d}"
            new_frame_id = f"{market}__{creator_slug}__{video_id}__{tc_file}"
            dest_path    = os.path.join(out_dir, f"{new_frame_id}.jpg")
            rel_path     = os.path.join("frames", market, creator_slug, f"{new_frame_id}.jpg")

            # Check if already in DB
            row = conn.execute(
                "SELECT frame_id, timecode, export_status FROM frames WHERE frame_id = ?",
                (new_frame_id,),
            ).fetchone()

            if row:
                parts = new_frame_id.split("__")
                result_frames.append({
                    "frame_id":          new_frame_id,
                    "timecode":          row["timecode"],
                    "timestamp_seconds": frame_ts_f,
                    "export_status":     row["export_status"],
                    "img_url":           f"/frames/{parts[0]}/{parts[1]}/{new_frame_id}.jpg",
                })
                continue

            shutil.copy2(img_path, dest_path)
            conn.execute("""
                INSERT INTO frames (frame_id, video_id, timestamp_seconds, timecode,
                                    export_status, file_path)
                VALUES (?, ?, ?, ?, 'unreviewed', ?)
            """, (new_frame_id, video_id, frame_ts_f, tc_display, rel_path))

            parts = new_frame_id.split("__")
            result_frames.append({
                "frame_id":          new_frame_id,
                "timecode":          tc_display,
                "timestamp_seconds": frame_ts_f,
                "export_status":     "unreviewed",
                "img_url":           f"/frames/{parts[0]}/{parts[1]}/{new_frame_id}.jpg",
            })

        conn.commit()

    # Always include the original frame
    if not any(f["frame_id"] == frame_id for f in result_frames):
        parts = frame_id.split("__")
        result_frames.append({
            "frame_id":          frame_id,
            "timecode":          frame["timecode"],
            "timestamp_seconds": ts,
            "export_status":     frame["export_status"] if "export_status" in frame.keys() else "unreviewed",
            "img_url":           f"/frames/{parts[0]}/{parts[1]}/{frame_id}.jpg",
        })

    result_frames.sort(key=lambda f: f["timestamp_seconds"])
    conn.close()
    return jsonify({"status": "ok", "frames": result_frames})


@app.post("/api/frame/<frame_id>/toggle")
def toggle_frame(frame_id: str):
    conn = get_db()
    row = conn.execute(
        "SELECT export_status FROM frames WHERE frame_id = ?", (frame_id,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    new_status = _TOGGLE.get(row["export_status"], "shortlisted")
    conn.execute(
        "UPDATE frames SET export_status = ? WHERE frame_id = ?", (new_status, frame_id)
    )
    conn.commit()
    conn.close()
    return jsonify({"export_status": new_status})


# ---------------------------------------------------------------------------
# API: update video themes
# ---------------------------------------------------------------------------

@app.post("/api/video/<video_id>/themes")
def update_themes(video_id: str):
    data = request.get_json(silent=True) or {}
    themes = data.get("themes", [])
    themes_str = ",".join(t.strip() for t in themes if t.strip())

    conn = get_db()
    conn.execute("UPDATE videos SET themes = ? WHERE video_id = ?", (themes_str, video_id))
    conn.commit()
    conn.close()
    return jsonify({"themes": themes})


# ---------------------------------------------------------------------------
# API: export market frames
#
# mode="new"  — export_status = 'shortlisted' only (not yet exported)
# mode="all"  — export_status IN ('shortlisted', 'exported') (re-export everything)
#
# Per-run steps:
#   1. Determine next export_round for this market
#   2. Create exports/export__{market}__{date}__round{N}/
#   3. Copy each source frame; mark export_status on success or failure
#   4. Write manifest.csv
#   5. Return summary JSON
# ---------------------------------------------------------------------------

_CSV_FIELDS = [
    "frame_id", "creator_name", "market", "video_title", "video_url",
    "timecode", "themes", "export_round", "first_exported_at",
    "this_exported_at", "resolution", "high_res_status", "avatar_filename",
]


@app.post("/api/market/<market_code>/export")
def export_market(market_code: str):
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "new")  # "new" | "all" | "retry"

    conn = get_db()

    if not conn.execute("SELECT 1 FROM videos WHERE market = ? LIMIT 1", (market_code,)).fetchone():
        conn.close()
        return jsonify({"error": "market not found"}), 404

    if mode == "all":
        status_filter = "AND f.export_status IN ('shortlisted', 'exported')"
    elif mode == "retry":
        status_filter = "AND f.export_status = 'export_failed'"
    else:
        status_filter = "AND f.export_status = 'shortlisted'"

    frames = conn.execute(f"""
        SELECT
            f.frame_id, f.timecode, f.resolution, f.exported_at,
            v.creator_name, v.creator_slug, v.market,
            v.video_title, v.video_url, v.themes
        FROM frames f
        JOIN videos v ON f.video_id = v.video_id
        WHERE v.market = ? {status_filter}
        ORDER BY v.creator_name, f.timestamp_seconds
    """, (market_code,)).fetchall()
    frames_list = [dict(f) for f in frames]

    if not frames_list:
        conn.close()
        return jsonify({"error": "no frames to export"}), 400

    row = conn.execute("""
        SELECT MAX(f.export_round)
        FROM frames f JOIN videos v ON f.video_id = v.video_id
        WHERE v.market = ?
    """, (market_code,)).fetchone()
    export_round = (row[0] or 0) + 1
    conn.close()

    date_str    = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder_name = f"export__{market_code}__{date_str}__round{export_round}"
    export_dir  = os.path.join(EXPORTS_DIR, folder_name)
    os.makedirs(export_dir, exist_ok=True)

    job_id = str(uuid.uuid4())
    with _export_jobs_lock:
        _export_jobs[job_id] = {
            "status":                 "running",
            "total":                  len(frames_list),
            "current":                0,
            "exported":               0,
            "failed":                 0,
            "cancelled":              False,
            "current_frame_id":       None,
            "current_frame_timecode": None,
            "results":                {},
            "folder":                 folder_name,
            "export_round":           export_round,
        }

    # Build slug → avatar_path map for use inside the export job
    with open(YAML_PATH, encoding="utf-8") as f:
        _yd = yaml.safe_load(f) or {}
    export_avatar_map: dict[str, str | None] = {
        c.get("creator_slug", ""): c.get("avatar_path")
        for m in _yd.get("markets", [])
        for c in m.get("creators", [])
    }

    threading.Thread(
        target=_run_export_job,
        args=(job_id, market_code, frames_list, export_round,
              export_dir, folder_name, export_avatar_map),
        daemon=True,
    ).start()

    return jsonify({
        "job_id":       job_id,
        "total":        len(frames_list),
        "folder":       folder_name,
        "export_round": export_round,
    })


@app.get("/api/export/job/<job_id>")
def export_job_status(job_id: str):
    with _export_jobs_lock:
        job = dict(_export_jobs.get(job_id, {}))
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.post("/api/export/job/<job_id>/cancel")
def cancel_export_job(job_id: str):
    with _export_jobs_lock:
        if job_id not in _export_jobs:
            return jsonify({"error": "not found"}), 404
        _export_jobs[job_id]["cancelled"] = True
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# API: delete video
# ---------------------------------------------------------------------------

@app.delete("/api/video/<video_id>")
def delete_video(video_id: str):
    conn = get_db()

    video = conn.execute(
        "SELECT video_id, video_url, video_title, market, creator_slug FROM videos WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    if not video:
        conn.close()
        return jsonify({"error": "not found"}), 404

    counts = conn.execute("""
        SELECT
            SUM(CASE WHEN export_status = 'shortlisted'   THEN 1 ELSE 0 END) AS shortlisted,
            SUM(CASE WHEN export_status = 'exported'      THEN 1 ELSE 0 END) AS exported,
            SUM(CASE WHEN export_status = 'export_failed' THEN 1 ELSE 0 END) AS failed
        FROM frames WHERE video_id = ?
    """, (video_id,)).fetchone()

    n_shortlisted = counts["shortlisted"] or 0
    n_exported    = counts["exported"]    or 0
    n_failed      = counts["failed"]      or 0

    if n_shortlisted or n_exported or n_failed:
        conn.close()
        return jsonify({
            "error":       "blocked",
            "shortlisted": n_shortlisted,
            "exported":    n_exported,
            "failed":      n_failed,
        }), 409

    frame_ids = [r["frame_id"] for r in conn.execute(
        "SELECT frame_id FROM frames WHERE video_id = ?", (video_id,)
    ).fetchall()]

    market       = video["market"].lower()
    creator_slug = video["creator_slug"]
    video_url    = video["video_url"]
    video_title  = video["video_title"]

    # Update YAML first — if this fails, nothing is deleted
    try:
        _remove_video_from_yaml(video_url)
    except Exception as exc:
        conn.close()
        return jsonify({"error": f"Failed to update configuration: {exc}"}), 500

    # Remove DB records
    conn.execute("DELETE FROM frames WHERE video_id = ?", (video_id,))
    conn.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
    conn.commit()
    conn.close()

    # Delete extracted frame images (best-effort)
    frame_dir = os.path.join(FRAMES_DIR, market, creator_slug)
    for fid in frame_ids:
        try:
            p = os.path.join(frame_dir, f"{fid}.jpg")
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass

    # Delete source video file (best-effort, any extension)
    raw_dir = os.path.join(FRAMES_DIR, "_raw_downloads")
    for ext in ("mp4", "webm", "mkv"):
        p = os.path.join(raw_dir, f"{video_id}.{ext}")
        try:
            if os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass

    return jsonify({"ok": True, "video_title": video_title})


# ---------------------------------------------------------------------------
# Manage page
# ---------------------------------------------------------------------------

@app.route("/manage")
def manage():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    themes = cfg.get("available_themes", ["cultural", "social", "economic"])

    with open(YAML_PATH, encoding="utf-8") as f:
        yaml_data = yaml.safe_load(f) or {}
    markets_setup = yaml_data.get("markets", [])

    creators_by_market = {
        m["code"]: [
            {"name": c.get("name", ""), "slug": c.get("creator_slug", "")}
            for c in m.get("creators", [])
        ]
        for m in markets_setup
    }

    conn = get_db()
    rows = conn.execute(
        "SELECT video_url, video_title, video_id FROM videos"
        " WHERE video_url IS NOT NULL AND video_title IS NOT NULL AND video_title != ''"
    ).fetchall()
    conn.close()
    url_to_title   = {r["video_url"]: r["video_title"] for r in rows}
    url_to_videoid = {r["video_url"]: r["video_id"]    for r in rows}

    return render_template(
        "manage.html",
        themes=themes,
        markets_setup=markets_setup,
        known_markets=KNOWN_MARKETS,
        creators_by_market=creators_by_market,
        url_to_title=url_to_title,
        url_to_videoid=url_to_videoid,
    )


@app.post("/api/manage/submit")
def manage_submit():
    data = request.get_json(silent=True) or {}

    mode           = data.get("mode", "new")  # "new" | "existing"
    market_code    = data.get("market", "").strip().upper()
    video_urls_raw = data.get("video_urls", "").strip()

    if not market_code:
        return jsonify({"error": "Market is required."}), 400

    # ── Existing creator: append more videos ─────────────────────────────────
    if mode == "existing":
        creator_slug = data.get("creator_slug", "").strip()
        if not creator_slug:
            return jsonify({"error": "Please select a creator."}), 400
        if not video_urls_raw:
            return jsonify({"error": "At least one video URL is required."}), 400

        # Look up the creator name for progress display
        creator_name = ""
        with _yaml_lock:
            with open(YAML_PATH, encoding="utf-8") as f:
                yaml_lookup = yaml.safe_load(f) or {}
        for m in yaml_lookup.get("markets", []):
            if m.get("code") == market_code:
                for c in m.get("creators", []):
                    if c.get("creator_slug") == creator_slug:
                        creator_name = c.get("name", creator_slug)
                        break

        if not creator_name:
            return jsonify({"error": "Creator not found in this market."}), 400

        job_id = str(uuid.uuid4())
        with _jobs_lock:
            _jobs[job_id] = {
                "mode":              "existing",
                "status":            "running",
                "phase":             "starting",
                "message":           "Starting…",
                "creator_name":      creator_name,
                "market_code":       market_code,
                "discovered_videos": [],
                "video_count":       0,
                "log_tail":          "",
                "error":             None,
            }

        threading.Thread(
            target=_run_add_videos_job,
            args=(job_id, market_code, creator_slug, video_urls_raw),
            daemon=True,
        ).start()

        return jsonify({"job_id": job_id})

    # ── New creator ──────────────────────────────────────────────────────────
    creator_name  = data.get("creator_name", "").strip()
    creator_slug  = data.get("creator_slug", "").strip()
    themes        = [t for t in data.get("themes", []) if t]
    input_method  = data.get("input_method", "videos")
    channel_url   = data.get("channel_url", "").strip()

    if not creator_name:
        return jsonify({"error": "Creator name is required."}), 400
    if not creator_slug:
        return jsonify({"error": "Creator slug is required."}), 400
    if not re.match(r"^[a-z0-9_]+$", creator_slug):
        return jsonify({"error": "Slug may only contain lowercase letters, numbers, and underscores."}), 400
    if input_method == "channel" and not channel_url:
        return jsonify({"error": "Channel URL is required."}), 400
    if input_method == "videos" and not video_urls_raw:
        return jsonify({"error": "At least one video URL is required."}), 400

    # Duplicate slug check
    with _yaml_lock:
        with open(YAML_PATH, encoding="utf-8") as f:
            yaml_check = yaml.safe_load(f) or {}
    for m in yaml_check.get("markets", []):
        if m.get("code") == market_code:
            for c in m.get("creators", []):
                if c.get("creator_slug") == creator_slug:
                    return jsonify({
                        "error": (
                            f'A creator with slug "{creator_slug}" already exists in {market_code}. '
                            f'Switch to "Add videos to existing creator" to add more videos.'
                        )
                    }), 400

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "mode":              "new",
            "status":            "running",
            "phase":             "starting",
            "message":           "Starting…",
            "creator_name":      creator_name,
            "market_code":       market_code,
            "discovered_videos": [],
            "video_count":       0,
            "log_tail":          "",
            "error":             None,
        }

    threading.Thread(
        target=_run_add_creator_job,
        args=(job_id, market_code, creator_name, creator_slug,
              themes, input_method, channel_url, video_urls_raw),
        daemon=True,
    ).start()

    return jsonify({"job_id": job_id})


@app.get("/api/manage/job/<job_id>")
def manage_job_status(job_id: str):
    with _jobs_lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


if __name__ == "__main__":
    app.run(debug=True, port=5001)

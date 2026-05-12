from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import yaml
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, render_template, request, send_file, url_for

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "database.sqlite")
FRAMES_DIR = os.path.join(BASE_DIR, "frames")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")

YAML_PATH   = os.path.join(BASE_DIR, "videos.yaml")
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")

KNOWN_MARKETS = ["AU", "BR", "DE", "EU", "FR", "ID", "IN", "JP", "UK", "US"]

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

_export_jobs: dict[str, dict] = {}
_export_jobs_lock = threading.Lock()

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
                    export_round: int, export_dir: str, folder_name: str) -> None:
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
                        themes: list[str], video_urls: list[str]) -> None:
    """Append a new creator+videos under market_code in videos.yaml. Non-destructive."""
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
        "default_themes": themes,
        "videos": [{"url": u} for u in video_urls],
    })

    with open(YAML_PATH, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)


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

        _update_job(job_id,
                    status="complete", phase="complete",
                    message="Done",
                    log_tail=_read_log_tail(),
                    video_count=len(video_urls))

    except Exception as exc:
        _update_job(job_id, status="failed", error=str(exc))


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

    status_clause = SHOW_CLAUSES.get(show, "")
    creator_clause = "AND v.creator_name = ?" if creator_filter else ""
    creator_params: list = [creator_filter] if creator_filter else []

    video_rows = conn.execute(f"""
        SELECT
            v.video_id, v.video_title, v.creator_name, v.creator_slug,
            v.themes, v.market,
            COUNT(f.frame_id)                                                    AS total_frames,
            SUM(CASE WHEN f.export_status = 'unreviewed'    THEN 1 ELSE 0 END)  AS unreviewed,
            SUM(CASE WHEN f.export_status = 'shortlisted'   THEN 1 ELSE 0 END)  AS shortlisted,
            SUM(CASE WHEN f.export_status = 'exported'      THEN 1 ELSE 0 END)  AS exported,
            SUM(CASE WHEN f.export_status = 'export_failed' THEN 1 ELSE 0 END)  AS failed
        FROM videos v
        LEFT JOIN frames f ON v.video_id = f.video_id
        WHERE v.market = ?
        {creator_clause}
        GROUP BY v.video_id
        ORDER BY v.creator_name, v.date_added
    """, [market_code] + creator_params).fetchall()

    groups: list[dict] = []
    current_creator = None
    creator_block: dict | None = None

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
        }

        if vrow["creator_name"] != current_creator:
            current_creator = vrow["creator_name"]
            creator_block = {"creator_name": current_creator, "videos": []}
            groups.append(creator_block)

        creator_block["videos"].append(video)

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
    "this_exported_at", "resolution", "high_res_status",
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
            v.creator_name, v.market, v.video_title, v.video_url, v.themes
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

    threading.Thread(
        target=_run_export_job,
        args=(job_id, market_code, frames_list, export_round, export_dir, folder_name),
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

    return render_template(
        "manage.html",
        themes=themes,
        markets_setup=markets_setup,
        known_markets=KNOWN_MARKETS,
    )


@app.post("/api/manage/submit")
def manage_submit():
    data = request.get_json(silent=True) or {}

    market_code    = data.get("market", "").strip().upper()
    creator_name   = data.get("creator_name", "").strip()
    creator_slug   = data.get("creator_slug", "").strip()
    themes         = [t for t in data.get("themes", []) if t]
    input_method   = data.get("input_method", "videos")
    channel_url    = data.get("channel_url", "").strip()
    video_urls_raw = data.get("video_urls", "").strip()

    if not market_code:
        return jsonify({"error": "Market is required."}), 400
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

    job_id = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "running",
            "phase": "starting",
            "message": "Starting…",
            "creator_name": creator_name,
            "market_code": market_code,
            "discovered_videos": [],
            "video_count": 0,
            "log_tail": "",
            "error": None,
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

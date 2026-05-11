from __future__ import annotations

import csv
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
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
    "this_exported_at",
    # resolution and high_res_status are placeholders until Step 7.5
    # (Playwright high-res capture). Remove/update when that ships.
    "resolution", "high_res_status",
]


@app.post("/api/market/<market_code>/export")
def export_market(market_code: str):
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "new")  # "new" | "all"

    conn = get_db()

    # Verify market exists
    if not conn.execute("SELECT 1 FROM videos WHERE market = ? LIMIT 1", (market_code,)).fetchone():
        conn.close()
        return jsonify({"error": "market not found"}), 404

    # Determine which frames to include
    if mode == "all":
        status_filter = "AND f.export_status IN ('shortlisted', 'exported')"
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

    if not frames:
        conn.close()
        return jsonify({"error": "no frames to export"}), 400

    # Next export round for this market
    row = conn.execute("""
        SELECT MAX(f.export_round)
        FROM frames f JOIN videos v ON f.video_id = v.video_id
        WHERE v.market = ?
    """, (market_code,)).fetchone()
    export_round = (row[0] or 0) + 1

    # Create export folder
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder_name = f"export__{market_code}__{date_str}__round{export_round}"
    export_dir = os.path.join(EXPORTS_DIR, folder_name)
    os.makedirs(export_dir, exist_ok=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    results: dict[str, dict] = {}
    manifest_rows: list[dict] = []

    for frame in frames:
        frame_id = frame["frame_id"]
        parts = frame_id.split("__")
        market_lower, creator_slug = parts[0], parts[1]
        src = os.path.join(FRAMES_DIR, market_lower, creator_slug, frame_id + ".jpg")
        dst = os.path.join(export_dir, frame_id + ".jpg")

        try:
            shutil.copy2(src, dst)

            first_exported_at = frame["exported_at"] or now

            conn.execute("""
                UPDATE frames SET
                    export_status = 'exported',
                    exported_at   = ?,
                    export_round  = ?,
                    export_error  = NULL
                WHERE frame_id = ?
            """, (now, export_round, frame_id))

            manifest_rows.append({
                "frame_id":          frame_id,
                "creator_name":      frame["creator_name"],
                "market":            frame["market"],
                "video_title":       frame["video_title"],
                "video_url":         frame["video_url"] or "",
                "timecode":          frame["timecode"],
                "themes":            frame["themes"] or "",
                "export_round":      export_round,
                "first_exported_at": first_exported_at,
                "this_exported_at":  now,
                "resolution":        frame["resolution"] or "640x360",
                "high_res_status":   "low_res_only",
            })

            results[frame_id] = {"export_status": "exported"}

        except Exception as exc:
            error_msg = str(exc)
            conn.execute("""
                UPDATE frames SET
                    export_status = 'export_failed',
                    export_error  = ?
                WHERE frame_id = ?
            """, (error_msg, frame_id))
            results[frame_id] = {"export_status": "export_failed", "error": error_msg}

    # Write manifest.csv
    csv_path = os.path.join(export_dir, "manifest.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        writer.writerows(manifest_rows)

    conn.commit()
    conn.close()

    exported = sum(1 for v in results.values() if v["export_status"] == "exported")
    failed   = sum(1 for v in results.values() if v["export_status"] == "export_failed")

    return jsonify({
        "export_round": export_round,
        "folder":       folder_name,
        "exported":     exported,
        "failed":       failed,
        "results":      results,
    })


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

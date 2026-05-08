from __future__ import annotations

import json
import os
import sqlite3

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "database.sqlite")
FRAMES_DIR = os.path.join(BASE_DIR, "frames")

app = Flask(__name__)


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
            COUNT(DISTINCT v.creator_name)    AS creator_count,
            COUNT(DISTINCT v.video_id)        AS video_count,
            COUNT(f.frame_id)                 AS frame_count,
            SUM(CASE WHEN f.status = 'unreviewed' THEN 1 ELSE 0 END)  AS unreviewed_count,
            SUM(CASE WHEN f.status = 'shortlisted' THEN 1 ELSE 0 END) AS shortlisted_count
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

@app.route("/market/<market_code>")
def review(market_code: str):
    conn = get_db()

    # Verify market exists
    exists = conn.execute(
        "SELECT 1 FROM videos WHERE market = ? LIMIT 1", (market_code,)
    ).fetchone()
    if not exists:
        conn.close()
        abort(404)

    show = request.args.get("show", "")  # unreviewed | all | shortlisted
    creator_filter = request.args.get("creator", "")
    theme_filter = request.args.get("theme", "")

    # Market-level stats
    stats = conn.execute("""
        SELECT
            COUNT(DISTINCT v.video_id)        AS video_count,
            COUNT(f.frame_id)                 AS frame_count,
            SUM(CASE WHEN f.status = 'unreviewed' THEN 1 ELSE 0 END)  AS unreviewed_count,
            SUM(CASE WHEN f.status = 'shortlisted' THEN 1 ELSE 0 END) AS shortlisted_count
        FROM videos v
        LEFT JOIN frames f ON v.video_id = f.video_id
        WHERE v.market = ?
    """, (market_code,)).fetchone()

    # Default show filter: unreviewed if any exist, otherwise all
    if not show:
        show = "unreviewed" if (stats["unreviewed_count"] or 0) > 0 else "all"

    # All creators in this market (for dropdown)
    creators = [r["creator_name"] for r in conn.execute(
        "SELECT DISTINCT creator_name FROM videos WHERE market = ? ORDER BY creator_name",
        (market_code,)
    ).fetchall()]

    # All themes (derived from all videos in this market)
    all_theme_rows = conn.execute(
        "SELECT DISTINCT themes FROM videos WHERE market = ? AND themes != ''",
        (market_code,)
    ).fetchall()
    all_themes: set[str] = set()
    for row in all_theme_rows:
        all_themes.update(t.strip() for t in (row["themes"] or "").split(",") if t.strip())
    all_themes_list = sorted(all_themes)

    # Build WHERE clause for frames based on show filter
    status_clause = {
        "unreviewed":  "AND status = 'unreviewed'",
        "shortlisted": "AND status = 'shortlisted'",
    }.get(show, "")

    creator_clause = "AND v.creator_name = ?" if creator_filter else ""
    creator_params: list = [creator_filter] if creator_filter else []

    # Fetch videos in this market with their frames
    video_rows = conn.execute(f"""
        SELECT
            v.video_id, v.video_title, v.creator_name, v.creator_slug,
            v.themes, v.market,
            COUNT(f.frame_id) AS total_frames,
            SUM(CASE WHEN f.status = 'unreviewed' THEN 1 ELSE 0 END) AS unreviewed,
            SUM(CASE WHEN f.status = 'shortlisted' THEN 1 ELSE 0 END) AS shortlisted
        FROM videos v
        LEFT JOIN frames f ON v.video_id = f.video_id
        WHERE v.market = ?
        {creator_clause}
        GROUP BY v.video_id
        ORDER BY v.creator_name, v.date_added
    """, [market_code] + creator_params).fetchall()

    # For each video, fetch matching frames
    groups: list[dict] = []
    current_creator = None
    creator_block: dict | None = None

    for vrow in video_rows:
        video_id = vrow["video_id"]
        video_themes = [t.strip() for t in (vrow["themes"] or "").split(",") if t.strip()]

        # Theme filter: skip video if it has no matching theme
        if theme_filter and theme_filter not in video_themes:
            continue

        frames = conn.execute(f"""
            SELECT frame_id, timestamp_seconds, timecode, status, file_path
            FROM frames
            WHERE video_id = ? {status_clause}
            ORDER BY timestamp_seconds
        """, (video_id,)).fetchall()

        # Skip video entirely if no frames match the current filter
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
# API: toggle shortlist
# ---------------------------------------------------------------------------

@app.post("/api/frame/<frame_id>/toggle")
def toggle_frame(frame_id: str):
    conn = get_db()
    row = conn.execute("SELECT status FROM frames WHERE frame_id = ?", (frame_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"error": "not found"}), 404

    new_status = "unreviewed" if row["status"] == "shortlisted" else "shortlisted"
    conn.execute("UPDATE frames SET status = ? WHERE frame_id = ?", (new_status, frame_id))
    conn.commit()
    conn.close()
    return jsonify({"status": new_status})


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


if __name__ == "__main__":
    app.run(debug=True, port=5001)

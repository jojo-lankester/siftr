from __future__ import annotations

import csv
import os
import shutil
import sqlite3
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, render_template, request, send_file, url_for

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "database.sqlite")
FRAMES_DIR = os.path.join(BASE_DIR, "frames")
EXPORTS_DIR = os.path.join(BASE_DIR, "exports")

app = Flask(__name__)


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


if __name__ == "__main__":
    app.run(debug=True, port=5001)

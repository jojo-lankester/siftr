"""
First-time setup for SIFTR.

Creates database.sqlite with the full schema.
Safe to run multiple times — exits cleanly if the database already exists.

Usage:
    python setup.py
"""

import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "database.sqlite")


def main() -> None:
    db_existed = os.path.exists(DB_PATH)

    if db_existed:
        # Check whether the tables are already there
        conn = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()

        if "videos" in tables and "frames" in tables:
            print(f"Database already initialised at {DB_PATH}")
            print("Nothing to do. Delete database.sqlite to start fresh.")
            sys.exit(0)

    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS videos (
            video_id        TEXT PRIMARY KEY,
            video_url       TEXT,
            video_title     TEXT,
            date_added      TEXT,
            creator_name    TEXT,
            creator_slug    TEXT DEFAULT '',
            market          TEXT,
            themes          TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS frames (
            frame_id            TEXT PRIMARY KEY,
            video_id            TEXT,
            timestamp_seconds   REAL,
            timecode            TEXT,
            resolution          TEXT,
            status              TEXT DEFAULT 'unreviewed',
            file_path           TEXT,
            extraction_date     TEXT,
            export_status       TEXT DEFAULT 'unreviewed',
            exported_at         TEXT,
            export_round        INTEGER,
            export_error        TEXT
        );
    """)

    conn.commit()
    conn.close()

    print(f"Database initialised at {DB_PATH}")


if __name__ == "__main__":
    main()

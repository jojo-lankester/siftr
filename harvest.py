"""
Top-level harvest command for SIFTR.
Runs the download module then the frame extraction module in sequence.

Usage:
    python harvest.py                  # full harvest from videos.txt
    python harvest.py --retry-failed   # retry failed downloads, then extract
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys

from download import FAILED_URLS_PATH, VIDEOS_YAML, run_harvest
from extract_frames import run_extraction

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "database.sqlite")


def _check_db() -> None:
    if not os.path.exists(_DB_PATH):
        print("Database not initialised. Please run: python setup.py")
        sys.exit(1)
    conn = sqlite3.connect(_DB_PATH)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    conn.close()
    if "videos" not in tables or "frames" not in tables:
        print("Database not initialised. Please run: python setup.py")
        sys.exit(1)


def main() -> None:
    _check_db()

    parser = argparse.ArgumentParser(description="SIFTR full harvest pipeline")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Download from logs/failed_urls.txt instead of videos.txt",
    )
    args = parser.parse_args()

    source = FAILED_URLS_PATH if args.retry_failed else VIDEOS_YAML
    run_harvest(source)
    run_extraction()


if __name__ == "__main__":
    main()

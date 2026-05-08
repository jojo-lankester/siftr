"""
Top-level harvest command for SIFTR.
Runs the download module then the frame extraction module in sequence.

Usage:
    python harvest.py                  # full harvest from videos.txt
    python harvest.py --retry-failed   # retry failed downloads, then extract
"""

from __future__ import annotations

import argparse

from download import FAILED_URLS_PATH, VIDEOS_TXT, run_harvest
from extract_frames import run_extraction


def main() -> None:
    parser = argparse.ArgumentParser(description="SIFTR full harvest pipeline")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Download from logs/failed_urls.txt instead of videos.txt",
    )
    args = parser.parse_args()

    source = FAILED_URLS_PATH if args.retry_failed else VIDEOS_TXT
    run_harvest(source)
    run_extraction()


if __name__ == "__main__":
    main()

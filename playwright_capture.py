"""
playwright_capture — standalone module for high-res YouTube frame capture.

Public API:
    capture_frame(video_url, timestamp_seconds, output_path, *, headless, timeout_ms)

No SIFTR-specific logic. Takes a URL, a timestamp, an output path, returns True.
Raises RuntimeError with a descriptive message on any failure.
"""

from __future__ import annotations

import os

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


def capture_frame(
    video_url: str,
    timestamp_seconds: int,
    output_path: str,
    *,
    headless: bool = True,
    timeout_ms: int = 60_000,
) -> bool:
    """
    Navigate to video_url, seek to timestamp_seconds, capture a 1920×1080
    fullscreen screenshot of the video frame, save to output_path as JPEG.

    Returns True on success.
    Raises RuntimeError with a clear message on failure.
    """
    sep = "&" if "?" in video_url else "?"
    url = f"{video_url}{sep}t={timestamp_seconds}s"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
            page = ctx.new_page()
            page.set_default_timeout(timeout_ms)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_selector("#movie_player", timeout=20_000)
                _dismiss_consent(page)
                _seek_and_pause(page, timestamp_seconds)
                _fullscreen(page)
                page.wait_for_timeout(2_000)

                os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
                page.screenshot(path=output_path, type="jpeg", quality=95)

            finally:
                browser.close()

    except PlaywrightTimeout as exc:
        raise RuntimeError(f"Timed out after {timeout_ms // 1000}s — video may be unavailable or slow to load.") from exc
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    if not os.path.exists(output_path):
        raise RuntimeError("Screenshot was not written to disk.")
    if os.path.getsize(output_path) < 10_000:
        raise RuntimeError(
            f"Screenshot is only {os.path.getsize(output_path)} bytes — "
            "capture may have hit a blank frame or YouTube error page."
        )

    return True


# ── Private helpers ───────────────────────────────────────────────────────────

def _dismiss_consent(page) -> None:
    """Dismiss YouTube's GDPR cookie/consent banner if it appears."""
    page.wait_for_timeout(3_000)
    selectors = [
        "ytd-consent-bump-v2-lightbox button:has-text('Reject all')",
        "ytd-consent-bump-v2-lightbox button:has-text('Accept all')",
        "ytd-consent-bump-v2-lightbox button",
        "button:has-text('Reject all')",
        "button:has-text('Accept all')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click()
                page.wait_for_timeout(1_500)
                return
        except Exception:
            continue


def _seek_and_pause(page, seconds: int) -> None:
    page.evaluate(f"""
        const p = document.querySelector('#movie_player');
        if (p && p.seekTo) {{
            p.seekTo({seconds}, true);
            p.pauseVideo();
        }}
    """)
    page.wait_for_timeout(1_500)


def _fullscreen(page) -> None:
    """Fullscreen the YouTube player so the video fills the full 1920×1080 viewport."""
    page.locator("#movie_player").click()
    page.wait_for_timeout(300)
    page.keyboard.press("f")
    page.wait_for_timeout(2_000)

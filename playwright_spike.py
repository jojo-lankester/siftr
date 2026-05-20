"""
Playwright spike — preserved for reference only. NOT part of the active codebase.

This was a proof-of-concept for automated high-res YouTube frame capture.
The approach was abandoned in favour of manual screenshotting via YouTube links
(see REVIEW.md — "Playwright high-res capture removal"). Kept here in case the
team revisits automated capture in future.
"""

import os
import sys
from PIL import Image
from playwright.sync_api import sync_playwright

# ── Test parameters (CLI args override hardcoded defaults) ───────────────────
VIDEO_URL  = sys.argv[1] if len(sys.argv) > 1 else "https://www.youtube.com/watch?v=b6kajtaCGoY"
TIMESTAMP  = int(sys.argv[2]) if len(sys.argv) > 2 else 30
OUTPUT     = os.path.join(os.path.dirname(__file__), "spike_test2.jpg")

# ── Helpers ───────────────────────────────────────────────────────────────────

def accept_consent(page):
    """Dismiss cookie / consent banners if present."""
    # Wait up to 4s for a banner to potentially appear
    page.wait_for_timeout(4000)

    selectors = [
        # YouTube consent lightbox (EU) — most specific first
        "ytd-consent-bump-v2-lightbox button:has-text('Reject all')",
        "ytd-consent-bump-v2-lightbox button:has-text('Accept all')",
        "ytd-consent-bump-v2-lightbox button",
        # Generic fallbacks
        "button[aria-label*='Reject all']",
        "button[aria-label*='Accept all']",
        "button:has-text('Reject all')",
        "button:has-text('Accept all')",
        "button:has-text('I agree')",
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                print(f"  Cookie/consent banner found — clicking: {sel}")
                btn.click()
                page.wait_for_timeout(1500)
                return
        except Exception:
            continue
    print("  No cookie/consent banner detected.")


def handle_age_gate(page):
    """Handle 'Sign in to confirm your age' gates."""
    try:
        btn = page.locator("button:has-text('I\\'m not a robot'), button:has-text('Sign in'), yt-confirm-dialog-renderer button").first
        if btn.is_visible(timeout=2000):
            print("  Age gate detected — cannot auto-bypass. Continuing anyway.")
    except Exception:
        pass


def seek_and_pause(page, seconds):
    """Seek via JavaScript player API and pause."""
    page.evaluate(f"""
        const player = document.querySelector('#movie_player') || window.yt?.player?.Application?.create?.();
        if (player && player.seekTo) {{
            player.seekTo({seconds}, true);
            player.pauseVideo();
        }}
    """)
    page.wait_for_timeout(1500)


def go_fullscreen(page):
    """Fullscreen the YouTube player so the video fills the 1920x1080 viewport."""
    # Click the player to ensure it has keyboard focus
    page.locator("#movie_player").click()
    page.wait_for_timeout(300)
    # 'f' is YouTube's fullscreen toggle shortcut
    page.keyboard.press("f")
    page.wait_for_timeout(2000)   # wait for fullscreen transition to complete


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    print("=" * 60)
    print("SIFTR Playwright spike — YouTube high-res frame capture")
    print("=" * 60)
    print(f"URL:       {VIDEO_URL}")
    print(f"Timestamp: {TIMESTAMP}s")
    print(f"Output:    {OUTPUT}")
    print()

    with sync_playwright() as p:
        print("Opening browser (headed, 1920x1080)...")
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(
            viewport={"width": 1920, "height": 1080},
        )
        page = ctx.new_page()

        # Navigate with ?t= so YouTube cues to the right position immediately
        sep = "&" if "?" in VIDEO_URL else "?"
        url_with_t = f"{VIDEO_URL}{sep}t={TIMESTAMP}s"
        print(f"Navigating to {url_with_t} ...")
        page.goto(url_with_t, wait_until="domcontentloaded", timeout=30000)

        print("Waiting for player to appear...")
        page.wait_for_selector("#movie_player", timeout=20000)

        print("Handling consent / cookie banners...")
        accept_consent(page)

        print("Handling age gate (if any)...")
        handle_age_gate(page)

        # Give the player a moment to settle after any banner dismissal
        page.wait_for_timeout(2000)

        print(f"Seeking to {TIMESTAMP}s and pausing...")
        seek_and_pause(page, TIMESTAMP)

        print("Going fullscreen so video fills 1920x1080 viewport...")
        go_fullscreen(page)

        # Extra wait: let the frame fully render at the new position
        print("Waiting for frame to render...")
        page.wait_for_timeout(2000)

        # In fullscreen the video element fills the viewport — screenshot the page
        print("Capturing screenshot...")
        page.screenshot(path=OUTPUT, type="jpeg", quality=95)

        print("Closing browser...")
        browser.close()

    # ── Report ────────────────────────────────────────────────────────────────
    print()
    print("─" * 60)
    print("RESULT")
    print("─" * 60)

    if not os.path.exists(OUTPUT):
        print("ERROR: output file not created.")
        sys.exit(1)

    size_kb = os.path.getsize(OUTPUT) / 1024
    img = Image.open(OUTPUT)
    w, h = img.size
    target_w, target_h = 1920, 1080

    print(f"File:       {OUTPUT}")
    print(f"Resolution: {w} x {h}  (target: {target_w} x {target_h})")
    print(f"File size:  {size_kb:.0f} KB")

    res_ok   = (w >= target_w and h >= target_h)
    size_ok  = size_kb > 50   # a blank or UI-chrome-only shot is typically very small

    if res_ok and size_ok:
        print("Verdict:    ✓ Looks like a usable high-res frame.")
    elif not res_ok:
        print(f"Verdict:    ✗ Resolution too low — got {w}x{h}, need {target_w}x{target_h}.")
    else:
        print(f"Verdict:    ? Resolution OK but file is small ({size_kb:.0f} KB) — may be blank or UI chrome.")

    print()
    print(f"Open {os.path.basename(OUTPUT)} to inspect the result.")


if __name__ == "__main__":
    run()

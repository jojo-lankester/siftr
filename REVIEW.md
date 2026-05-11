# SIFTR Review Log

Running log of decisions, settings, and behaviours to revisit. Update this file whenever a "let's come back to this" decision is made during the build, and commit it alongside the related changes.

---

## Items to review with Jack

Jack built a similar tool last year and has hands-on experience of what "useful frames" actually look like in practice. These items need his input before we lock anything in.

### Frame extraction tuning
- **Scene-change threshold** (currently `0.3` — hardcoded in `extract_frames.py`) — is this picking up the right moments? Too sensitive / not sensitive enough?
- **Dedup threshold** (currently `90%` in `config.yaml`) — talking-head videos collapsed from 111 frames to 6. Is this too aggressive? Reference examples: `b6kajtaCGoY` (music video, 64 frames) and `boGz13J7IYw` (talking head, 6 frames after dedup).
- **Uniform sampling interval** (currently `30s` in `config.yaml`) — is this the right cadence for static or slow-moving videos?
- **Min frames per video before fallback triggers** (currently `10` in `config.yaml`) — should scene-change videos with fewer than 10 cuts always fall back to uniform, or is there a better heuristic?

### Resolution
- Currently set to `640x360` in dev mode due to SABR/yt-dlp limitations — flagged with a `DEV MODE` comment in `config.yaml`.
- Must return to `1920x1080` minimum before production use.
- Need to resolve high-resolution YouTube downloading — Jack's tool last year achieved this. Worth comparing approaches directly.

### Download approach
- Current approach: yt-dlp with `android` player client + Chrome cookies — only producing 360p due to SABR streaming enforcement.
- Jack's tool got high-res downloads but had reliability issues. What library/approach did he use?
- PO Token approach is the documented next step if Jack's approach isn't viable. See: <https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide>

---

## Data structure decisions (Step 4)

The market → creator → videos hierarchy in `videos.yaml` was chosen deliberately:

- **Matches the team's actual workflow** — the team works market-by-market; market is the natural top-level grouping for both harvest runs and UI browsing.
- **Supports future UI input patterns** — e.g. "select market, paste a list of creator channel URLs" as a one-step onboarding flow, rather than requiring per-video URL entry.
- **Enables auto-fetch features** — once channel-level discovery is built (Step 8), the YAML structure already has the right anchor points (market + creator) to attach auto-discovered videos to.
- **`creator_slug` is explicit**, not derived from the name, so renaming a creator (e.g. rebranding) doesn't silently break folder paths or frame filenames.

---

## Items to review when we have real data

- **Volume per video** (currently averaging 50–100 frames) — is this overwhelming or appropriate for a review session? The `review_batch_size` config setting (currently `30`) controls how many are shown at once, but the total pool size matters too.
- **UI hierarchy** — is grouping by creator → video the right structure, or should market be the top level?
- **Default filter behaviour** — does starting on "unreviewed" and offering "all / shortlisted" tabs match how designers actually work through a batch?

---

## Export data model (added before Step 6)

### Two-tier capture design

Frames are captured at two different quality levels at different points in the workflow:

1. **Low-res review copy** — extracted by `extract_frames.py` via FFmpeg at the resolution available from yt-dlp (currently 360p in dev, target 1080p in production). Stored in `frames/`. Used for review in the UI.
2. **High-res export copy** — captured at export time only, via Playwright headless browser, navigating to the exact YouTube timecode and screenshotting at full resolution. Stored in `exports/`. This is the deliverable.

The high-res capture is deferred until the designer has confirmed their shortlist, so we avoid spending time/bandwidth on frames that won't be used. Playwright is not built yet — the data model and UI are prepared here (Step 7.5 will build it).

### Multi-round export workflow

Export rounds allow designers to add new creators/videos to an existing market and run multiple export batches without losing track of which frames came from which round:

- Each export run increments `export_round` on all frames it successfully captures.
- Previously exported frames (earlier rounds) remain `exported` and are not re-captured.
- New shortlisted frames from a new harvest run start as `shortlisted` and are picked up in the next export.
- The harvest log line tells the designer what changed: "12 new videos · 847 new frames added. 34 previously shortlisted frames unchanged."

### Four export states (frames.export_status)

| State | Meaning | UI |
|---|---|---|
| `unreviewed` | Not yet reviewed by a designer | No border |
| `shortlisted` | Shortlisted, pending high-res export | Yellow border |
| `exported` | High-res export captured successfully | Green border + ✓ |
| `export_failed` | Export attempted but failed (Playwright error, timecode out of range, etc.) | Red border + ✗, clickable to retry |

**Retry behaviour**: clicking an `export_failed` frame re-queues it to `shortlisted`. The next export run will re-attempt it and either set it to `exported` or `export_failed` again with the new error message in `export_error`.

**Exported frames are not re-togglable** via the grid click — only the export process can set/unset `exported`. This prevents accidental deselection of frames that have already been delivered.

### DB columns added (pre-Step 6 migration)

- `export_status TEXT` — canonical status field: unreviewed / shortlisted / exported / export_failed
- `exported_at TEXT` — ISO timestamp of most recent successful export
- `export_round INTEGER` — which export batch this frame was captured in
- `export_error TEXT` — error message from last failed export attempt (null if none)

The legacy `status` column (unreviewed / shortlisted) remains in the DB but is no longer read or written by the app.

---

## Open features deferred from spec

- **High-res Playwright capture** (Step 7.5) — navigate to YouTube timecode, screenshot at 1080p+ into `exports/`.
- **Browser input UI** (Step 8, after Step 7 export and Step 7.5 Playwright capture) — add markets, creators, and videos through the browser instead of editing `videos.yaml` by hand. Deferred deliberately: the priority is completing the core review-and-export loop first, so there's a working end-to-end tool to test in practice before adding convenience features.
  - Auto-discovery of top videos from a creator's channel (currently a later-stage feature) would integrate naturally into this input UI when built — selecting a creator could surface their top videos for the user to pick from.
  - Auto-discovery implementation notes (for future review):
    - Currently uses yt-dlp flat playlist scraping to find videos. YouTube Data API v3 is a possible upgrade if yt-dlp scraping becomes unreliable or rate-limited.
    - The 18-month window and "top 3" video defaults are tunable (config-level changes). Expect Jack/team feedback to refine these once real channels are added.
    - No edit or delete in the manage UI yet — add-only by design. To be revisited once Mat and the team have used it in practice and we know what "oops" scenarios actually come up.
- **AI-assisted moment detection** (Step 9 in tech spec) — automatic flagging of high-impact frames using a vision model.
- **Similar-frame suggestions** — using embedding similarity to surface frames visually related to ones already shortlisted.

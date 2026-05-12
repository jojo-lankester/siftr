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

## Lessons from real-world use

- **Cookies-from-browser was originally mandatory** — first deployment to Mat's Linux machine showed this was overly aggressive: Chrome wasn't installed, so the harvester hung before downloading anything. Fixed by making cookies a fallback triggered only on auth failures, with a browser availability check upfront. General principle: configuration that's necessary on one machine should be a fallback, not a default, until proven needed across machines.

- **Creator avatar fetch strategy** — avatars are fetched via yt-dlp at creator-add time. When a video URL (not a channel URL) is used as the source, yt-dlp returns video info without channel thumbnails; the fix is to follow `channel_url`/`uploader_url` from that info dict and re-fetch at channel level. Thumbnails at channel level come back as an array with varying resolutions — we pick the highest by `width × height`. Downloaded to `creator_avatars/{market}/{slug}.jpg` via `urllib.request` (no `requests` dependency). A daemon thread backfills avatars for creators added before this feature was built.

---

## Completed steps (for reference)

- **Download robustness fixes** — `download.py` now:
  - Checks browser availability before attempting to load cookies. If the configured browser isn't installed, logs a clear warning and continues without cookies rather than hanging or crashing.
  - Uses a "try simple first" strategy: attempt 1 is always made without cookies (faster, no browser dependency). Cookies are only loaded as a fallback if attempt 1 fails with an auth-related error (HTTP 403, sign-in required, etc.). The RETRY_DELAYS (30s, 60s) only apply to genuine network/server failures, not to the cookie escalation step.
  - `config.yaml` now has a comment above `cookies_browser` explaining the optional nature and when cookies are used.

- **Step 8: Browser input UI** — `/manage` page built. Supports adding markets, creators, and videos via the browser. Channel URL auto-discovery uses yt-dlp flat playlist scraping. Implementation notes:
  - Channel URL must point to the `/videos` tab (e.g. `@username/videos`) — the channel root returns tab entries, not videos.
  - **`view_count` is always null with `extract_flat=True`** — the "top 3 most-viewed" sort is a no-op; videos come back in YouTube's default channel order. Revisit if ordering matters.
  - YouTube Data API v3 is a possible upgrade if yt-dlp scraping becomes unreliable or rate-limited.
  - The 18-month window and "top 3" defaults are tunable in code. Expect Jack/team feedback once real channels are added.

---

## Open features deferred from spec

- **Frame detail panel** (Step 6) — click-to-expand view of an individual frame (full-size image, timecode, status, YouTube link). Deferred in favour of completing the export loop first.
- **High-res Playwright capture** (Step 7.5) — **built and integrated**. Export now runs Playwright headless Chromium, seeks to the frame timecode, fullscreens, and screenshots at 1920×1080. Implementation notes for future tuning:
  - Capture runs sequentially (one browser instance at a time) with a configurable delay between frames (`playwright_delay_between_captures` in `config.yaml`, default 2s). Could be parallelised if speed becomes a priority, but risks YouTube rate-limiting and memory pressure.
  - The 60s per-frame timeout (`playwright_timeout_seconds` in `config.yaml`) was chosen conservatively. Real-world captures take ~12–15s on a fast connection. Tune down once we have production data.
  - High-res capture relies on the video still being available on YouTube at export time (not at the time of original review). If a video is deleted or made private between harvest and export, that frame will fail with `export_failed`. Worth flagging to Mat and the team — not much we can do about it except note it in the manifest.
  - Cancel during capture is safe: already-captured frames are kept, in-progress capture finishes its current frame (up to the timeout), then stops. No partial files are left behind.
  - `manifest.csv` now records `resolution: 1920x1080` and `high_res_status: success` for all included rows (only successful captures are included).
- **Edit/delete in Manage UI** — `/manage` supports adding new creators and adding more videos to existing ones. To remove a creator or video, edit `videos.yaml` directly. To be revisited once Mat and the team have used it in practice.
- **Manage page two-mode flow** — the form has an explicit toggle: "Add new creator" (default) and "Add videos to existing creator". Switching modes preserves the market selection. "New creator" mode validates that the slug is unique in that market and errors with a helpful redirect hint if not. "Existing creator" mode appends new URLs and skips duplicates (idempotent). A startup migration (`_merge_yaml_duplicates`) merges any creators that share a slug in the same market, preserving the avatar from whichever entry has one.
- **AI-assisted moment detection** (Step 9 in tech spec) — automatic flagging of high-impact frames using a vision model.
- **Similar-frame suggestions** — using embedding similarity to surface frames visually related to ones already shortlisted.
- **Batch delete** — no mechanism yet for "delete all unreviewed videos from creator X". Could be useful if a creator turns out to be unsuitable after a large harvest. Deferred until the team has enough real-world data to know whether the scenario comes up.

---

## Video deletion (hard delete)

### Decision: hard delete only

Soft-delete (flagging videos as hidden rather than removing them) was considered and deliberately rejected. Reasons:
- The primary use case is removing unsuitable videos to free up UI space and disk. A soft-deleted video that's still on disk provides no disk hygiene benefit.
- The data model has no concept of "archived" or "hidden" status for videos.
- Soft-delete would require audit logic, UI to show/restore hidden videos, and an eventual purge step. That's three features where one suffices.
- If a video is accidentally deleted and needs to come back, re-adding the URL and re-running harvest is a full recovery.

### Block rule: shortlisted, exported, or failed frames prevent deletion

Any frame with status `shortlisted`, `exported`, or `export_failed` blocks the delete. The UI shows the count of each and tells the designer to un-shortlist before proceeding. Rationale:
- `exported` frames represent delivered work — deleting the video would delete frames that may already be in the client's hands.
- `shortlisted` frames represent a designer's curation decision. Accidental deletion would silently discard that work.
- `export_failed` frames are waiting for a retry — deleting them would lose the retry queue.

### Operation order for safety

YAML is updated first. If YAML update fails, the delete is aborted (DB and files are untouched). DB deletion follows, then file deletion (best-effort — orphaned files from a failed file delete are not ideal but not critical). This order means the "worst" partial failure state is: YAML is clean but some orphaned frame files remain on disk. No user-visible data is lost.

### Items to review

- **No undo**: deletion is immediate and permanent. Worth confirming with Mat and the team whether a short grace window (e.g. "deleted — restore?" with a 10s timer) would be valuable, or whether the confirmation dialog is sufficient protection.

---

## "Show nearby frames" — redefined behaviour (Step 6 panel work)

Originally designed to filter the existing grid to frames within ±60s of the selected frame (using a server-side `nearby_frame_ids` API). **Redefined** to instead extract new fine-grained frames from the source video on demand.

### New behaviour

When the designer clicks "Show nearby frames ±5s" in the frame detail panel:

1. The client calls `POST /api/frame/<frame_id>/extract_nearby`.
2. The server finds the source video at `frames/_raw_downloads/<video_id>.mp4` (or `.webm`, `.mkv`).
3. FFmpeg extracts frames at 2 fps across a ±5s window (10s total, ~20 frames) into a temp directory.
4. Each frame is assigned an ID in the standard format: `{market}__{creator_slug}__{video_id}__{HH-MM-SS.mmm}`.
5. New frames are written to `frames/{market}/{creator_slug}/` and inserted into the `frames` table as `unreviewed`.
6. **Idempotency**: if >5 frames already exist in the ±5s window for that video (from a previous extraction), FFmpeg is skipped and the existing frames are returned.
7. The grid filters to show only the returned frames. Any frames new to the DOM are injected as card elements. The nav panel is hidden, and a dark banner appears: "NEARBY · Frames around {timecode} ±5s · {video title}".
8. "Return to market" restores the full grid and nav panel.

### Why this was chosen over the original approach

The original filter-existing-grid approach was limited to frames that happened to have been extracted during the initial harvest. Harvest uses scene-change detection + uniform sampling at 30s intervals — not designed to capture subtle moments within a few seconds of a chosen frame. The new approach gives designers fine-grained control: see exactly what's around an interesting moment at 0.5s resolution, without pre-extracting every frame of every video.

### Items to review with the team

- **FFmpeg extraction rate**: currently 2 fps (0.5s intervals, ~20 frames per ±5s call). Could tune this — finer or coarser — depending on how much variation there is frame-to-frame.
- **Window size**: ±5s is conservative. A ±10s or ±15s window might be more useful for B-roll review.
- **Source video presence**: extraction only works if the raw video file exists at `frames/_raw_downloads/`. If the machine running SIFTR doesn't have the raw downloads (e.g. a deployment where only frames were copied), this feature silently fails with a "Source video file not found" error. Worth deciding whether raw downloads should always travel with the frame files.
- **Storage growth**: each "Show nearby frames" call adds up to ~20 new frame images (~50–200KB each at q:v 5). With heavy use, this could add up. No cleanup mechanism currently exists for extracted nearby frames.

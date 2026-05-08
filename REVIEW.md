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

## Items to review when we have real data

- **Volume per video** (currently averaging 50–100 frames) — is this overwhelming or appropriate for a review session? The `review_batch_size` config setting (currently `30`) controls how many are shown at once, but the total pool size matters too.
- **UI hierarchy** — is grouping by creator → video the right structure, or should market be the top level?
- **Default filter behaviour** — does starting on "unreviewed" and offering "all / shortlisted" tabs match how designers actually work through a batch?

---

## Open features deferred from spec

- **AI-assisted moment detection** (Step 9 in tech spec) — automatic flagging of high-impact frames using a vision model.
- **YouTube channel auto-discovery** (Step 8 in tech spec) — automatically surfacing top videos from a creator's channel rather than requiring manual URL entry.
- **Similar-frame suggestions** — using embedding similarity to surface frames visually related to ones already shortlisted.

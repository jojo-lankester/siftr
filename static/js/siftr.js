"use strict";

const STATUS_CLASSES = ["unreviewed", "shortlisted"];

// Increment or decrement a status-bar count badge (creates/removes as needed)
function nudgeCount(showVal, delta) {
  const btn = document.querySelector(`[data-show-val="${showVal}"]`);
  if (!btn) return;
  let span = btn.querySelector(".status-count");
  const current = span ? parseInt(span.textContent, 10) : 0;
  const next    = Math.max(0, current + delta);
  if (next > 0) {
    if (!span) {
      span = document.createElement("span");
      span.className = "status-count";
      btn.appendChild(span);
    }
    span.textContent = next;
  } else if (span) {
    span.remove();
  }
}

// ── Shortlist toggle (event delegation — works for dynamically added cards) ───

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".frame");
  if (!btn) return;

  const prevStatus = btn.dataset.exportStatus;
  const frameId    = btn.dataset.frameId;

  const res = await fetch(`/api/frame/${encodeURIComponent(frameId)}/toggle`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });

  if (!res.ok) return;
  const { export_status: newStatus } = await res.json();

  btn.dataset.exportStatus = newStatus;
  STATUS_CLASSES.forEach((cls) => btn.classList.remove(cls));
  btn.classList.add(newStatus.replace(/_/g, "-"));

  const card = btn.closest(".frame-card");
  if (card) card.dataset.exportStatus = newStatus;

  btn.setAttribute("aria-label",
    `Frame at ${btn.querySelector(".frame-meta")?.textContent?.trim() ?? ""}, ${newStatus.replace(/_/g, " ")}`
  );

  updateVideoStats(btn);
  nudgeCount(prevStatus,  -1);
  nudgeCount(newStatus,   +1);
});


// Recount shortlisted frames within a video block and refresh the stat span
function updateVideoStats(frameBtn) {
  const videoBlock = frameBtn.closest(".video-block");
  if (!videoBlock) return;

  let shortlisted = 0;
  videoBlock.querySelectorAll(".frame").forEach((f) => {
    if (f.dataset.exportStatus === "shortlisted") shortlisted++;
  });

  const countEl = videoBlock.querySelector(".video-frame-count");
  if (!countEl) return;

  setStatSpan(countEl, "stat-shortlisted", shortlisted, "shortlisted");
}

function setStatSpan(parent, cls, count, label) {
  let span = parent.querySelector(`.${cls}`);
  if (count > 0) {
    if (!span) {
      span = document.createElement("span");
      span.className = cls;
      parent.appendChild(span);
    }
    span.textContent = `· ${count} ${label}`;
  } else if (span) {
    span.remove();
  }
}


// ── Theme chip editing ────────────────────────────────────────────────────────

document.querySelectorAll(".video-themes").forEach((container) => {
  const videoId = container.dataset.videoId;
  const addBtn = container.querySelector(".theme-add-btn");
  const addInput = container.querySelector(".theme-add-input");

  function getThemes() {
    return [...container.querySelectorAll(".theme-chip")].map(
      (chip) => chip.firstChild.textContent.trim()
    );
  }

  async function saveThemes(themes) {
    await fetch(`/api/video/${encodeURIComponent(videoId)}/themes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ themes }),
    });
  }

  function addChip(tag) {
    const chip = document.createElement("span");
    chip.className = "theme-chip";
    chip.innerHTML = `${tag}<button class="theme-remove" data-tag="${tag}" aria-label="Remove ${tag}">×</button>`;
    chip.querySelector(".theme-remove").addEventListener("click", removeChip);
    container.insertBefore(chip, addBtn);
  }

  function removeChip(e) {
    e.target.closest(".theme-chip").remove();
    saveThemes(getThemes());
  }

  container.querySelectorAll(".theme-remove").forEach((btn) => {
    btn.addEventListener("click", removeChip);
  });

  addBtn.addEventListener("click", () => {
    addInput.classList.remove("hidden");
    addInput.focus();
  });

  function commitInput() {
    const tag = addInput.value.trim().toLowerCase().replace(/\s+/g, "-");
    if (tag) {
      addChip(tag);
      saveThemes(getThemes());
    }
    addInput.value = "";
    addInput.classList.add("hidden");
  }

  addInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") commitInput();
    if (e.key === "Escape") { addInput.value = ""; addInput.classList.add("hidden"); }
  });

  addInput.addEventListener("blur", commitInput);
});


// ── Creator collapse ──────────────────────────────────────────────────────────

document.querySelectorAll(".creator-toggle").forEach((btn) => {
  btn.addEventListener("click", () => {
    const section        = btn.closest(".creator-section");
    const body           = document.getElementById(btn.getAttribute("aria-controls"));
    const collapsedStats = section.querySelector(".creator-collapsed-stats");
    const isOpen         = btn.getAttribute("aria-expanded") === "true";

    btn.setAttribute("aria-expanded", String(!isOpen));
    if (body) body.hidden = isOpen;
    if (collapsedStats) collapsedStats.hidden = !isOpen;
  });
});


// ── Back to top ───────────────────────────────────────────────────────────────

(function initBackToTop() {
  const btn = document.getElementById("back-to-top");
  if (!btn) return;

  window.addEventListener("scroll", () => {
    btn.hidden = window.scrollY < 200;
  }, { passive: true });

  btn.addEventListener("click", () => {
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
}());


// ── Nav panel ─────────────────────────────────────────────────────────────────

(function initNavPanel() {
  const panel = document.getElementById("nav-panel");
  if (!panel) return;

  const topbar    = document.querySelector(".topbar");
  const filterBar = document.querySelector(".filter-bar");

  // ── Sticky offset ────────────────────────────────────────────────────────
  function updateStickyOffset() {
    const h = (topbar ? topbar.offsetHeight : 0) + (filterBar ? filterBar.offsetHeight : 0);
    document.documentElement.style.setProperty("--sticky-h", `${h}px`);
  }
  updateStickyOffset();
  window.addEventListener("resize", updateStickyOffset, { passive: true });

  // ── Creator expand/collapse in panel ────────────────────────────────────
  panel.querySelectorAll(".nav-creator-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      btn.closest(".nav-creator").classList.toggle("nav-creator--collapsed");
    });
  });

  // ── Video nav click ──────────────────────────────────────────────────────
  panel.querySelectorAll(".nav-video-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const videoId    = btn.dataset.videoId;
      const videoBlock = document.querySelector(`.video-block[data-video-id="${videoId}"]`);
      if (!videoBlock) return;

      // Expand creator section in grid if it's collapsed
      const creatorBody = videoBlock.closest(".creator-body");
      if (creatorBody && creatorBody.hidden) {
        const gridToggle = document.querySelector(`[aria-controls="${creatorBody.id}"]`);
        if (gridToggle) {
          gridToggle.setAttribute("aria-expanded", "true");
          creatorBody.hidden = false;
          const section = creatorBody.closest(".creator-section");
          const stats = section && section.querySelector(".creator-collapsed-stats");
          if (stats) stats.hidden = true;
        }
      }

      // Scroll to video block, clearing the sticky header
      const stickyH = parseFloat(
        getComputedStyle(document.documentElement).getPropertyValue("--sticky-h")
      ) || 0;
      const rect = videoBlock.getBoundingClientRect();
      window.scrollTo({ top: window.scrollY + rect.top - stickyH - 16, behavior: "smooth" });
    });
  });

  // ── Highlight currently-visible video ───────────────────────────────────
  const navBtnMap = new Map();
  panel.querySelectorAll(".nav-video-btn").forEach((btn) => {
    navBtnMap.set(btn.dataset.videoId, btn);
  });

  function updateHighlight() {
    const stickyH = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--sticky-h")
    ) || 0;
    let activeId = null;

    document.querySelectorAll(".video-block").forEach((block) => {
      if (block.getBoundingClientRect().top <= stickyH + 40) {
        activeId = block.dataset.videoId;
      }
    });

    navBtnMap.forEach((btn, id) => {
      btn.classList.toggle("nav-video-btn--active", id === activeId);
    });
  }

  window.addEventListener("scroll", updateHighlight, { passive: true });
  updateHighlight();
}());


// ── Detail panel ──────────────────────────────────────────────────────────────

(function initDetailPanel() {
  const panel              = document.getElementById("detail-panel");
  if (!panel) return;

  const closeBtn           = document.getElementById("detail-close-btn");
  const backBtn            = document.getElementById("detail-back-btn");
  const statusBadge        = document.getElementById("detail-status-badge");
  const detailImg          = document.getElementById("detail-img");
  const dpCreatorName      = document.getElementById("dp-creator-name");
  const dpHandle           = document.getElementById("dp-creator-handle");
  const dpMarket           = document.getElementById("dp-market");
  const dpVideoTitle       = document.getElementById("dp-video-title");
  const dpTimecode         = document.getElementById("dp-timecode");
  const dpStatus           = document.getElementById("dp-status");
  const dpFrameId          = document.getElementById("dp-frame-id");
  const dpThemes           = document.getElementById("dp-themes");
  const shortlistBtn       = document.getElementById("detail-shortlist-btn");
  const daNearby           = document.getElementById("da-nearby");
  const daNearbyText       = document.getElementById("da-nearby-text");
  const daNearbyError      = document.getElementById("da-nearby-error");
  const daVideo            = document.getElementById("da-video");
  const daCreator          = document.getElementById("da-creator");
  const daCreatorLabel     = document.getElementById("da-creator-label");
  const nearbyBanner       = document.getElementById("nearby-banner");
  const nearbyDesc         = document.getElementById("nearby-desc");
  const nearbyBackBtn      = document.getElementById("nearby-back-btn");
  const videoFilterBanner  = document.getElementById("video-filter-banner");
  const videoFilterDesc    = document.getElementById("video-filter-desc");
  const videoFilterBackBtn = document.getElementById("video-filter-back-btn");
  const navPanel           = document.getElementById("nav-panel");

  let current = null;

  // ── Return to market ──────────────────────────────────────────────────────────

  function returnToMarket() {
    nearbyBanner.hidden = true;
    videoFilterBanner.hidden = true;
    if (navPanel) navPanel.style.display = "";
    document.querySelectorAll(".frame-card, .video-block, .creator-section").forEach(el => {
      el.style.display = "";
    });
  }

  nearbyBackBtn.addEventListener("click", returnToMarket);
  videoFilterBackBtn.addEventListener("click", returnToMarket);

  // ── Create frame card (for dynamically extracted frames) ──────────────────────

  function createFrameCard(frameData, panelData) {
    const { frame_id, timecode, export_status, img_url } = frameData;
    const { videoId, videoTitle, creatorName, creatorSlug, market } = panelData;
    const es = export_status || "unreviewed";

    const card = document.createElement("div");
    card.className = "frame-card";
    card.dataset.frameId      = frame_id;
    card.dataset.timecode     = timecode;
    card.dataset.exportStatus = es;
    card.dataset.videoId      = videoId;
    card.dataset.videoTitle   = videoTitle;
    card.dataset.creatorName  = creatorName;
    card.dataset.creatorSlug  = creatorSlug;
    card.dataset.market       = market;
    card.dataset.imgUrl       = img_url;

    const frameBtn = document.createElement("button");
    frameBtn.className = `frame ${es.replace(/_/g, "-")}`;
    frameBtn.dataset.frameId      = frame_id;
    frameBtn.dataset.exportStatus = es;
    frameBtn.setAttribute("aria-label", `Frame at ${timecode}, ${es.replace(/_/g, " ")}`);
    frameBtn.innerHTML = `
      <div class="frame-img-wrap">
        <img class="frame-img" src="${img_url}" alt="Frame at ${timecode}" loading="lazy" />
        <div class="frame-state-overlay" aria-hidden="true"></div>
      </div>
      <div class="frame-meta mono">${timecode}</div>`;

    const expandBtn = document.createElement("button");
    expandBtn.className = "expand-btn";
    expandBtn.type = "button";
    expandBtn.setAttribute("aria-label", `Expand frame at ${timecode}`);
    expandBtn.textContent = "Expand";

    card.appendChild(frameBtn);
    card.appendChild(expandBtn);
    return card;
  }

  // ── Open / close ─────────────────────────────────────────────────────────────

  function openPanel(data) {
    current = data;
    renderPanel(data);
    panel.classList.add("is-open");
    document.addEventListener("keydown", onEscKey, { capture: true });
  }

  function closePanel() {
    panel.classList.remove("is-open");
    document.removeEventListener("keydown", onEscKey, { capture: true });
  }

  function onEscKey(e) {
    if (e.key === "Escape") { e.stopPropagation(); closePanel(); }
  }

  closeBtn.addEventListener("click", closePanel);
  backBtn.addEventListener("click", closePanel);

  document.addEventListener("click", (e) => {
    if (!panel.classList.contains("is-open")) return;
    if (panel.contains(e.target)) return;
    if (e.target.closest(".frame-card, .nav-panel, .video-themes")) return;
    closePanel();
  });

  // ── Render ───────────────────────────────────────────────────────────────────

  const STATUS_LABELS = {
    shortlisted: "Shortlisted",
    unreviewed:  "Unreviewed",
  };

  const BADGE_INFO = {
    shortlisted: { cls: "status--shortlisted", text: () => "Shortlisted" },
    unreviewed:  { cls: "",                    text: () => "" },
  };

  function timecodeToSeconds(tc) {
    const parts = tc.split(":");
    return parseInt(parts[0], 10) * 3600 + parseInt(parts[1], 10) * 60 + parseInt(parts[2], 10);
  }

  const ytLink = document.getElementById("da-youtube");

  function renderPanel(data) {
    const es = data.exportStatus;
    const bi = BADGE_INFO[es] || { cls: "", text: () => es };
    statusBadge.className = "detail-status-badge" + (bi.cls ? " " + bi.cls : "");
    statusBadge.textContent = bi.text();

    detailImg.classList.add("is-loading");
    detailImg.src = data.imgUrl;
    detailImg.alt = `Frame at ${data.timecode}`;
    detailImg.onload  = () => detailImg.classList.remove("is-loading");
    detailImg.onerror = () => detailImg.classList.remove("is-loading");

    dpCreatorName.textContent = data.creatorName;
    dpHandle.textContent = "@" + data.creatorName.replace(/\s+/g, "");
    dpMarket.textContent = data.market;
    dpVideoTitle.textContent = data.videoTitle;
    dpTimecode.textContent = data.timecode;
    dpStatus.textContent = STATUS_LABELS[es] || es;
    dpFrameId.textContent = data.frameId;

    if (ytLink) {
      const ts = timecodeToSeconds(data.timecode);
      ytLink.href = `https://www.youtube.com/embed/${data.videoId}?start=${ts}&autoplay=0`;
    }

    renderPanelThemes(data.videoId, data.themes);
    renderShortlistBtn(es);

    if (daCreatorLabel) daCreatorLabel.textContent = `All frames from ${data.creatorName}`;

    // Reset nearby button state
    daNearbyText.textContent = "Show nearby frames ±5s";
    daNearby.disabled = false;
    daNearbyError.hidden = true;
    daNearbyError.textContent = "";

    panel.scrollTop = 0;
  }

  // ── Themes ───────────────────────────────────────────────────────────────────

  function renderPanelThemes(videoId, themes) {
    dpThemes.innerHTML = "";

    const gridEl = document.querySelector(`.video-themes[data-video-id="${videoId}"]`);
    const live = gridEl
      ? [...gridEl.querySelectorAll(".theme-chip")].map(c => c.firstChild.textContent.trim())
      : themes;

    live.forEach(tag => dpThemes.appendChild(makeChip(tag, videoId)));

    const addBtn = document.createElement("button");
    addBtn.className = "theme-add-btn";
    addBtn.textContent = "+";
    addBtn.setAttribute("aria-label", "Add theme");
    dpThemes.appendChild(addBtn);

    const addInput = document.createElement("input");
    addInput.className = "theme-add-input hidden";
    addInput.type = "text";
    addInput.placeholder = "add theme…";
    addInput.maxLength = 30;
    dpThemes.appendChild(addInput);

    addBtn.addEventListener("click", () => { addInput.classList.remove("hidden"); addInput.focus(); });

    const commit = () => {
      const tag = addInput.value.trim().toLowerCase().replace(/\s+/g, "-");
      if (tag) { dpThemes.insertBefore(makeChip(tag, videoId), addBtn); savePanelThemes(videoId); }
      addInput.value = "";
      addInput.classList.add("hidden");
    };
    addInput.addEventListener("keydown", e => {
      if (e.key === "Enter") commit();
      if (e.key === "Escape") { addInput.value = ""; addInput.classList.add("hidden"); }
    });
    addInput.addEventListener("blur", commit);
  }

  function makeChip(tag, videoId) {
    const chip = document.createElement("span");
    chip.className = "theme-chip";
    chip.innerHTML = `${tag}<button class="theme-remove" data-tag="${tag}" aria-label="Remove ${tag}">×</button>`;
    chip.querySelector(".theme-remove").addEventListener("click", () => {
      chip.remove();
      savePanelThemes(videoId);
    });
    return chip;
  }

  async function savePanelThemes(videoId) {
    const themes = [...dpThemes.querySelectorAll(".theme-chip")]
      .map(c => c.firstChild.textContent.trim());
    await fetch(`/api/video/${encodeURIComponent(videoId)}/themes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ themes }),
    });
  }

  // ── Shortlist button ──────────────────────────────────────────────────────────

  function renderShortlistBtn(es) {
    shortlistBtn.className = "detail-shortlist-btn";
    shortlistBtn.removeAttribute("hidden");
    if (es === "unreviewed") {
      shortlistBtn.classList.add("action--shortlist");
      shortlistBtn.textContent = "Shortlist this frame";
    } else if (es === "shortlisted") {
      shortlistBtn.classList.add("action--unshortlist");
      shortlistBtn.textContent = "Remove from shortlist";
    } else {
      shortlistBtn.setAttribute("hidden", "");
    }
  }

  shortlistBtn.addEventListener("click", async () => {
    if (!current) return;

    const frameId    = current.frameId;
    const prevStatus = current.exportStatus;

    const res = await fetch(`/api/frame/${encodeURIComponent(frameId)}/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (!res.ok) return;
    const { export_status: newStatus } = await res.json();

    current.exportStatus = newStatus;

    const bi = BADGE_INFO[newStatus] || { cls: "", text: () => newStatus };
    statusBadge.className = "detail-status-badge" + (bi.cls ? " " + bi.cls : "");
    statusBadge.textContent = bi.text();
    dpStatus.textContent = STATUS_LABELS[newStatus] || newStatus;
    renderShortlistBtn(newStatus);

    const gridFrame = document.querySelector(`.frame[data-frame-id="${frameId}"]`);
    if (gridFrame) {
      gridFrame.dataset.exportStatus = newStatus;
      STATUS_CLASSES.forEach(cls => gridFrame.classList.remove(cls));
      gridFrame.classList.add(newStatus.replace(/_/g, "-"));
      gridFrame.setAttribute("aria-label",
        `Frame at ${current.timecode}, ${newStatus.replace(/_/g, " ")}`);
      updateVideoStats(gridFrame);
    }

    const gridCard = document.querySelector(`.frame-card[data-frame-id="${frameId}"]`);
    if (gridCard) gridCard.dataset.exportStatus = newStatus;

    nudgeCount(prevStatus, -1);
    nudgeCount(newStatus,  +1);
  });

  // ── Expand buttons (delegated — works for dynamically added cards) ────────────

  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".expand-btn");
    if (!btn) return;
    e.stopPropagation();
    const card = btn.closest(".frame-card");
    if (!card) return;

    const videoId = card.dataset.videoId;
    const gridEl  = document.querySelector(`.video-themes[data-video-id="${videoId}"]`);
    const themes  = gridEl
      ? [...gridEl.querySelectorAll(".theme-chip")].map(c => c.firstChild.textContent.trim())
      : [];

    openPanel({
      frameId:      card.dataset.frameId,
      timecode:     card.dataset.timecode,
      exportStatus: card.dataset.exportStatus,
      videoId,
      videoTitle:   card.dataset.videoTitle,
      creatorName:  card.dataset.creatorName,
      creatorSlug:  card.dataset.creatorSlug,
      market:       card.dataset.market,
      imgUrl:       card.dataset.imgUrl,
      themes,
    });
  });

  // ── Nearby frames — extract via FFmpeg ───────────────────────────────────────

  daNearby.addEventListener("click", async () => {
    if (!current) return;
    const panelData = { ...current };
    const { frameId, timecode, videoTitle } = current;

    daNearbyText.textContent = "Extracting frames…";
    daNearby.disabled = true;
    daNearbyError.hidden = true;
    daNearbyError.textContent = "";

    let result;
    try {
      const res = await fetch(`/api/frame/${encodeURIComponent(frameId)}/extract_nearby`, {
        method: "POST",
      });
      result = await res.json();

      if (!res.ok) {
        const msg = result.error || "Unknown error";
        if (msg === "source_video_missing") {
          daNearbyError.textContent = "Source video file not found for this frame.";
        } else if (msg.startsWith("ffmpeg_failed")) {
          daNearbyError.textContent = "FFmpeg failed: " + msg.slice("ffmpeg_failed: ".length);
        } else {
          daNearbyError.textContent = "Error: " + msg;
        }
        daNearbyError.hidden = false;
        daNearbyText.textContent = "Show nearby frames ±5s";
        daNearby.disabled = false;
        return;
      }
    } catch (err) {
      daNearbyError.textContent = "Network error — please try again.";
      daNearbyError.hidden = false;
      daNearbyText.textContent = "Show nearby frames ±5s";
      daNearby.disabled = false;
      return;
    }

    closePanel();

    // Find the frames-grid for this video to append any new cards
    const originCard = document.querySelector(`.frame-card[data-frame-id="${frameId}"]`);
    const framesGrid = originCard ? originCard.closest(".frames-grid") : null;

    const nearbyIds = new Set();
    (result.frames || []).forEach(f => {
      nearbyIds.add(f.frame_id);
      if (!document.querySelector(`.frame-card[data-frame-id="${f.frame_id}"]`)) {
        const card = createFrameCard(f, panelData);
        if (framesGrid) framesGrid.appendChild(card);
      }
    });

    document.querySelectorAll(".frame-card").forEach(card => {
      card.style.display = nearbyIds.has(card.dataset.frameId) ? "" : "none";
    });
    document.querySelectorAll(".video-block").forEach(block => {
      const hasVis = [...block.querySelectorAll(".frame-card")].some(c => c.style.display !== "none");
      block.style.display = hasVis ? "" : "none";
    });
    document.querySelectorAll(".creator-section").forEach(sec => {
      const hasVis = [...sec.querySelectorAll(".video-block")].some(b => b.style.display !== "none");
      sec.style.display = hasVis ? "" : "none";
    });

    videoFilterBanner.hidden = true;
    nearbyDesc.textContent = ` Frames around ${timecode} ±5s · ${videoTitle}`;
    nearbyBanner.hidden = false;
    if (navPanel) navPanel.style.display = "none";

    daNearbyText.textContent = "Show nearby frames ±5s";
    daNearby.disabled = false;
  });

  // ── All frames from video ─────────────────────────────────────────────────────

  daVideo.addEventListener("click", () => {
    if (!current) return;
    const { videoId, videoTitle, creatorName } = current;
    closePanel();

    document.querySelectorAll(".frame-card").forEach(card => {
      card.style.display = card.dataset.videoId === videoId ? "" : "none";
    });
    document.querySelectorAll(".video-block").forEach(block => {
      const hasVis = [...block.querySelectorAll(".frame-card")].some(c => c.style.display !== "none");
      block.style.display = hasVis ? "" : "none";
    });
    document.querySelectorAll(".creator-section").forEach(sec => {
      const hasVis = [...sec.querySelectorAll(".video-block")].some(b => b.style.display !== "none");
      sec.style.display = hasVis ? "" : "none";
    });

    nearbyBanner.hidden = true;
    videoFilterDesc.textContent = ` · ${videoTitle} · ${creatorName}`;
    videoFilterBanner.hidden = false;
    if (navPanel) navPanel.style.display = "none";
  });

  // ── All frames from creator ───────────────────────────────────────────────────

  daCreator.addEventListener("click", () => {
    if (!current) return;
    closePanel();
    const url = new URL(window.location.href);
    url.searchParams.set("show", "all");
    url.searchParams.delete("video_id");
    url.searchParams.delete("theme");
    url.searchParams.set("creator", current.creatorName);
    window.location.href = url.toString();
  });
}());


// ── Delete video ──────────────────────────────────────────────────────────────

(function initDeleteVideo() {
  function escHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  let overlayEl = null;
  let toastEl   = null;

  function getOverlay() {
    if (!overlayEl) {
      overlayEl = document.createElement("div");
      overlayEl.className = "siftr-dialog-overlay";
      document.body.appendChild(overlayEl);
    }
    return overlayEl;
  }

  function getToast() {
    if (!toastEl) {
      toastEl = document.createElement("div");
      toastEl.className = "siftr-toast";
      toastEl.setAttribute("role", "status");
      toastEl.setAttribute("aria-live", "polite");
      document.body.appendChild(toastEl);
    }
    return toastEl;
  }

  function showDeleteToast(msg) {
    const t = getToast();
    t.textContent = msg;
    t.classList.add("is-visible");
    setTimeout(() => t.classList.remove("is-visible"), 2800);
  }

  function openDialog(html) {
    const overlay = getOverlay();
    overlay.innerHTML = `<div class="siftr-dialog">${html}</div>`;
    overlay.style.display = "flex";
    return overlay;
  }

  function closeDialog() {
    const overlay = getOverlay();
    overlay.style.display = "none";
  }

  function showAlert(title, body) {
    return new Promise(resolve => {
      const overlay = openDialog(`
        <p class="siftr-dialog-title">${escHtml(title)}</p>
        <p class="siftr-dialog-body">${escHtml(body)}</p>
        <div class="siftr-dialog-actions">
          <button class="siftr-dialog-btn siftr-dialog-btn--ok" type="button">OK</button>
        </div>
      `);
      const ok = overlay.querySelector(".siftr-dialog-btn--ok");
      ok.focus();
      ok.addEventListener("click", () => { closeDialog(); resolve(); });
    });
  }

  function showConfirm(title, body) {
    return new Promise(resolve => {
      const overlay = openDialog(`
        <p class="siftr-dialog-title">${escHtml(title)}</p>
        <p class="siftr-dialog-body">${escHtml(body)}</p>
        <div class="siftr-dialog-actions">
          <button class="siftr-dialog-btn siftr-dialog-btn--cancel" type="button">Cancel</button>
          <button class="siftr-dialog-btn siftr-dialog-btn--delete" type="button">Delete video</button>
        </div>
      `);

      const cancelBtn = overlay.querySelector(".siftr-dialog-btn--cancel");
      const deleteBtn = overlay.querySelector(".siftr-dialog-btn--delete");

      cancelBtn.focus();
      cancelBtn.addEventListener("click", () => { closeDialog(); resolve(false); });
      deleteBtn.addEventListener("click", () => { closeDialog(); resolve(true); });

      function onKey(e) {
        if (e.key === "Escape") {
          document.removeEventListener("keydown", onKey, { capture: true });
          closeDialog();
          resolve(false);
        }
      }
      document.addEventListener("keydown", onKey, { capture: true });
    });
  }

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".video-delete-btn");
    if (!btn) return;

    const videoId    = btn.dataset.videoId;
    const videoTitle = btn.dataset.videoTitle;

    const confirmed = await showConfirm(
      `Delete '${videoTitle}'?`,
      "This will permanently remove the video, all its extracted frames, and all source files. This cannot be undone."
    );
    if (!confirmed) return;

    let res, data;
    try {
      res  = await fetch(`/api/video/${encodeURIComponent(videoId)}`, { method: "DELETE" });
      data = await res.json();
    } catch (err) {
      await showAlert("Delete failed", "Network error — please try again.");
      return;
    }

    if (res.status === 409) {
      const parts = [];
      if (data.shortlisted) parts.push(`${data.shortlisted} shortlisted`);
      if (data.exported)    parts.push(`${data.exported} exported`);
      if (data.failed)      parts.push(`${data.failed} failed`);
      await showAlert(
        `Can't delete '${videoTitle}' yet`,
        `It has ${parts.join(", ")} frame(s) that must be un-shortlisted before deleting.`
      );
      return;
    }

    if (!res.ok) {
      await showAlert("Delete failed", data.error || `Server error ${res.status}`);
      return;
    }

    // Remove from DOM — works on review page (.video-block) and manage page (li)
    const target = btn.closest(".video-block") || btn.closest("li");
    if (target) target.remove();

    showDeleteToast(`Deleted '${videoTitle}'`);
  });
}());

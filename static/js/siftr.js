"use strict";

// ── Export-status state classes ───────────────────────────────────────────────
const STATUS_CLASSES = ["unreviewed", "shortlisted", "exported", "export-failed"];

// ── Shortlist toggle ──────────────────────────────────────────────────────────

document.querySelectorAll(".frame").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const exportStatus = btn.dataset.exportStatus;

    // Exported frames cannot be toggled via the grid
    if (exportStatus === "exported") return;

    const frameId = btn.dataset.frameId;

    const res = await fetch(`/api/frame/${encodeURIComponent(frameId)}/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    if (!res.ok) return;
    const { export_status: newStatus } = await res.json();

    // Update DOM classes
    btn.dataset.exportStatus = newStatus;
    STATUS_CLASSES.forEach((cls) => btn.classList.remove(cls));
    btn.classList.add(newStatus.replace("_", "-"));

    // Update aria-label
    btn.setAttribute("aria-label",
      `Frame at ${btn.querySelector(".frame-meta")?.textContent?.trim() ?? ""}, ${newStatus.replace(/_/g, " ")}`
    );

    // Update state icon (✓ / ✗) — only present for exported/failed
    const overlay = btn.querySelector(".frame-state-overlay");
    if (overlay) {
      overlay.innerHTML =
        newStatus === "exported"      ? '<span class="state-icon">✓</span>' :
        newStatus === "export_failed" ? '<span class="state-icon">✗</span>' :
        "";
    }

    updateVideoStats(btn);
  });
});


// Recount all four statuses within a video block and refresh the stat spans
function updateVideoStats(frameBtn) {
  const videoBlock = frameBtn.closest(".video-block");
  if (!videoBlock) return;

  const counts = { shortlisted: 0, exported: 0, "export-failed": 0 };
  videoBlock.querySelectorAll(".frame").forEach((f) => {
    const s = f.dataset.exportStatus;
    if (s === "shortlisted")   counts.shortlisted++;
    if (s === "exported")      counts.exported++;
    if (s === "export_failed") counts["export-failed"]++;
  });

  const countEl = videoBlock.querySelector(".video-frame-count");
  if (!countEl) return;

  setStatSpan(countEl, "stat-shortlisted", counts.shortlisted, "shortlisted");
  setStatSpan(countEl, "stat-exported",    counts.exported,    "exported");
  setStatSpan(countEl, "stat-failed",      counts["export-failed"], "failed");
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

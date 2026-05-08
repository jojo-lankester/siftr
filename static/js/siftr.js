"use strict";

// ── Shortlist toggle ──────────────────────────────────────────────────────────

document.querySelectorAll(".frame").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const frameId = btn.dataset.frameId;

    const res = await fetch(`/api/frame/${encodeURIComponent(frameId)}/toggle`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });

    if (!res.ok) return;
    const { status } = await res.json();

    btn.dataset.status = status;
    btn.classList.toggle("shortlisted", status === "shortlisted");

    // Update shortlisted count in the video header
    updateShortlistCount(btn);
  });
});

function updateShortlistCount(frameBtn) {
  const videoBlock = frameBtn.closest(".video-block");
  if (!videoBlock) return;

  const shortlisted = videoBlock.querySelectorAll(".frame.shortlisted").length;
  let countEl = videoBlock.querySelector(".shortlisted-count");

  if (shortlisted > 0) {
    if (!countEl) {
      const frameCountEl = videoBlock.querySelector(".video-frame-count");
      countEl = document.createElement("span");
      countEl.className = "shortlisted-count";
      frameCountEl.appendChild(countEl);
    }
    countEl.textContent = `· ${shortlisted} shortlisted`;
  } else if (countEl) {
    countEl.remove();
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
    const chip = e.target.closest(".theme-chip");
    chip.remove();
    saveThemes(getThemes());
  }

  // Wire up existing remove buttons
  container.querySelectorAll(".theme-remove").forEach((btn) => {
    btn.addEventListener("click", removeChip);
  });

  // Show/hide add input
  addBtn.addEventListener("click", () => {
    addInput.classList.remove("hidden");
    addInput.focus();
  });

  addInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      const tag = addInput.value.trim().toLowerCase().replace(/\s+/g, "-");
      if (tag) {
        addChip(tag);
        saveThemes(getThemes());
      }
      addInput.value = "";
      addInput.classList.add("hidden");
    }
    if (e.key === "Escape") {
      addInput.value = "";
      addInput.classList.add("hidden");
    }
  });

  addInput.addEventListener("blur", () => {
    const tag = addInput.value.trim().toLowerCase().replace(/\s+/g, "-");
    if (tag) {
      addChip(tag);
      saveThemes(getThemes());
    }
    addInput.value = "";
    addInput.classList.add("hidden");
  });
});

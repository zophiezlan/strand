(() => {
  const dropZone = document.getElementById("drop");
  const fileInput = document.getElementById("file");
  const folderInput = document.getElementById("folder");
  const pickFolderLink = document.getElementById("pick-folder");
  const goBtn = document.getElementById("go");
  const statusEl = document.getElementById("status");
  const resultEl = document.getElementById("result");
  const previewEl = document.getElementById("preview");
  const previewImg = document.getElementById("preview-img");
  const seedValueEl = document.getElementById("seed-value");
  const rerollSameBtn = document.getElementById("reroll-same");
  const rerollNewBtn = document.getElementById("reroll-new");
  const downloadBtn = document.getElementById("download");
  const zipSummaryEl = document.getElementById("zip-summary");
  const statsEl = document.getElementById("stats");
  const statsLineEl = document.getElementById("stats-line");
  const suffixInput = document.getElementById("suffix");

  const IMAGE_SUFFIXES = new Set([".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"]);
  const SUPPORTED = [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".pdf", ".pptx", ".zip"];
  const MAX_BYTES = 25 * 1024 * 1024;
  const DEFAULT_SUFFIX = "-strand";

  /** Array of File objects currently selected for upload (always >= 0). */
  let currentFiles = [];
  // Defaults chosen from real-world testing: a single light hair best
  // mimics the photocopier-glass / laminator-pocket artefact, which is
  // the most recognisable "stray hair" look.
  let palette = "white";
  let intensity = "subtle";
  let lastSeed = null;

  /** The most recent result, held in memory so the explicit Download button
      can save it on click instead of the page auto-downloading. */
  let lastResultBlob = null;
  let lastResultName = null;
  let lastPreviewUrl = null;  // separate object URL for inline preview img

  // --- Chip selectors ---
  for (const group of ["palette", "intensity"]) {
    const groupEl = document.getElementById(group);
    groupEl.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      for (const c of groupEl.querySelectorAll(".chip")) {
        c.classList.toggle("selected", c === chip);
        c.setAttribute("aria-checked", c === chip ? "true" : "false");
      }
      if (group === "palette") palette = chip.dataset.value;
      else intensity = chip.dataset.value;
    });
  }

  // --- File / folder pickers, drop zone ---
  // Click on the drop zone opens the file picker, unless the click was on a
  // link inside it (the "pick a folder" affordance has its own handler).
  dropZone.addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    fileInput.click();
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length) setFiles(Array.from(fileInput.files));
  });
  pickFolderLink.addEventListener("click", (e) => {
    e.preventDefault();
    folderInput.click();
  });
  folderInput.addEventListener("change", () => {
    if (folderInput.files && folderInput.files.length) setFiles(Array.from(folderInput.files));
  });

  for (const evt of ["dragenter", "dragover"]) {
    dropZone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropZone.classList.add("dragover");
    });
  }
  for (const evt of ["dragleave", "dragend", "drop"]) {
    dropZone.addEventListener(evt, () => dropZone.classList.remove("dragover"));
  }
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      setFiles(Array.from(e.dataTransfer.files));
    }
  });

  function suffixOf(name) {
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  }

  function fileRelPath(f) {
    return f.webkitRelativePath || f.name;
  }

  function setFiles(files) {
    // Filter to supported types; surface a friendly message if everything got dropped.
    const accepted = files.filter((f) => SUPPORTED.includes(suffixOf(f.name)));
    if (!accepted.length) {
      const tried = files.map((f) => suffixOf(f.name) || "(no extension)").join(", ");
      showError(`None of those are supported (${tried}). Try ${SUPPORTED.join(", ")}.`);
      return;
    }

    const total = accepted.reduce((s, f) => s + f.size, 0);
    if (total > MAX_BYTES) {
      showError(`Selection is ${(total / (1024 * 1024)).toFixed(1)} MB — limit is 25 MB total.`);
      return;
    }

    currentFiles = accepted;
    lastSeed = null;
    dropZone.classList.add("has-file");

    const summary = describeSelection(accepted, total, files.length);
    dropZone.querySelector(".drop-text strong").textContent = summary.title;
    dropZone.querySelector(".drop-text span").textContent = summary.subtitle;

    goBtn.disabled = false;
    hideStatus();
    resultEl.hidden = true;
  }

  function describeSelection(accepted, totalBytes, originalCount) {
    const sizeKb = `${(totalBytes / 1024).toFixed(0)} KB`;
    if (accepted.length === 1) {
      return { title: accepted[0].name, subtitle: `${sizeKb} — ready` };
    }
    // Try to surface a common folder root when the user picked a directory.
    const rels = accepted.map(fileRelPath);
    const roots = new Set(rels.map((p) => p.split("/")[0]));
    const root = roots.size === 1 && rels.every((p) => p.includes("/")) ? roots.values().next().value : null;
    const skipped = originalCount - accepted.length;
    const skippedNote = skipped > 0 ? ` (${skipped} we can't handle will stay out)` : "";
    return {
      title: root ? `${root}/ — ${accepted.length} files` : `${accepted.length} files`,
      subtitle: `${sizeKb} — you'll get them back as a zip${skippedNote}`,
    };
  }

  function showError(msg) {
    statusEl.hidden = false;
    statusEl.className = "status error";
    statusEl.textContent = msg;
  }

  function showWorking(msg) {
    statusEl.hidden = false;
    statusEl.className = "status working";
    statusEl.textContent = msg;
  }

  function showInfo(msg) {
    statusEl.hidden = false;
    statusEl.className = "status";
    statusEl.textContent = msg;
  }

  function hideStatus() {
    statusEl.hidden = true;
    statusEl.className = "status";
    statusEl.textContent = "";
  }

  // --- Submit ---
  async function submit({ reuseSeed = false } = {}) {
    if (!currentFiles.length) return;
    goBtn.disabled = true;
    rerollSameBtn.disabled = true;
    rerollNewBtn.disabled = true;
    downloadBtn.disabled = true;
    previewEl.hidden = true;
    zipSummaryEl.hidden = true;
    statsEl.hidden = true;
    // Discard any prior result + preview URL before starting fresh.
    if (lastPreviewUrl) {
      URL.revokeObjectURL(lastPreviewUrl);
      lastPreviewUrl = null;
    }
    lastResultBlob = null;
    lastResultName = null;
    showWorking("Adding hair");

    try {
      const form = new FormData();
      for (const f of currentFiles) {
        // Preserve sub-paths for folder uploads via the third FormData arg.
        form.append("file", f, fileRelPath(f));
      }
      form.append("palette", palette);
      form.append("intensity", intensity);
      if (reuseSeed && lastSeed != null) {
        form.append("seed", String(lastSeed));
      }
      const suffixValue = (suffixInput.value || "").trim();
      // Only send if non-empty AND not the default — keeps the wire small.
      if (suffixValue && suffixValue !== DEFAULT_SUFFIX) {
        form.append("name_suffix", suffixValue);
      } else if (suffixValue === "") {
        // Explicit empty: user wants no suffix.
        form.append("name_suffix", "");
      }

      const res = await fetch("/strand", { method: "POST", body: form });

      if (!res.ok) {
        let detail = `Server returned ${res.status}.`;
        try {
          const body = await res.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) { /* not JSON; keep default */ }
        showError(detail);
        return;
      }

      const echoedSeed = res.headers.get("X-Strand-Seed");
      if (echoedSeed) {
        lastSeed = echoedSeed;
        seedValueEl.textContent = echoedSeed;
      }

      const haired = res.headers.get("X-Strand-Haired");
      const skipped = res.headers.get("X-Strand-Skipped");
      const errored = res.headers.get("X-Strand-Errored");
      const isZipResponse = haired != null || skipped != null;

      // Stats panel — hidden by default, opens on click for nerds who want
      // to know which morphologies got drawn and across how many pages.
      renderStats(res.headers.get("X-Strand-Stats"));

      const blob = await res.blob();
      const singleFile = currentFiles[0];
      const downloadName = downloadNameFrom(res, singleFile.name);
      const suffix = suffixOf(singleFile.name);

      // Stash for the explicit Download button — the page no longer
      // auto-fires the save dialog. The user looks at the preview first.
      lastResultBlob = blob;
      lastResultName = downloadName;

      resultEl.hidden = false;

      const previewSingleImage = currentFiles.length === 1 && IMAGE_SUFFIXES.has(suffix);

      if (isZipResponse) {
        const parts = [];
        if (haired) parts.push(`<strong>${haired}</strong> got hair`);
        if (skipped && Number(skipped) > 0) parts.push(`${skipped} left alone`);
        if (errored && Number(errored) > 0) parts.push(`${errored} had trouble`);
        zipSummaryEl.innerHTML = parts.join(" · ") +
          ` &nbsp;·&nbsp; there's a summary inside the zip if you want the details.`;
        zipSummaryEl.hidden = false;
        showInfo("Done — your zip's ready.");
      } else if (previewSingleImage) {
        lastPreviewUrl = URL.createObjectURL(blob);
        previewImg.src = lastPreviewUrl;
        previewEl.hidden = false;
        showInfo("Done. How does it look?");
      } else {
        showInfo("Done — ready when you are.");
      }
    } catch (err) {
      showError(`Network error: ${err.message || err}`);
    } finally {
      goBtn.disabled = false;
      rerollSameBtn.disabled = lastSeed == null;
      rerollNewBtn.disabled = false;
      downloadBtn.disabled = lastResultBlob == null;
    }
  }

  goBtn.addEventListener("click", () => submit({ reuseSeed: false }));
  rerollSameBtn.addEventListener("click", () => submit({ reuseSeed: true }));
  rerollNewBtn.addEventListener("click", () => submit({ reuseSeed: false }));
  downloadBtn.addEventListener("click", () => {
    if (lastResultBlob && lastResultName) {
      triggerDownload(lastResultBlob, lastResultName);
    }
  });

  function renderStats(headerValue) {
    if (!headerValue) { statsEl.hidden = true; return; }
    let s;
    try { s = JSON.parse(headerValue); } catch { statsEl.hidden = true; return; }

    const total = s.hairs || 0;
    if (total === 0) { statsEl.hidden = true; return; }

    const morphs = s.morphologies || {};
    // Order morphologies by count desc so the dominant one reads first.
    const named = Object.entries(morphs)
      .filter(([, n]) => n > 0)
      .sort((a, b) => b[1] - a[1])
      .map(([name, n]) => `${n} ${pluralize(name, n)}`);
    const morphStr = named.length ? ` (${named.join(", ")})` : "";

    const pages = s.pages_touched || 0;
    const pagesStr = pages > 1 ? ` across ${pages} pages` : "";

    statsLineEl.textContent = `${total} ${pluralize("hair", total)}${morphStr}${pagesStr}.`;
    statsEl.hidden = false;
  }

  function pluralize(word, n) {
    if (n === 1) return word;
    return word + "s";
  }

  function downloadNameFrom(res, fallback) {
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    if (m) return m[1];
    const i = fallback.lastIndexOf(".");
    return i < 0 ? `${fallback}-strand` : `${fallback.slice(0, i)}-strand${fallback.slice(i)}`;
  }

  function triggerDownload(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  // Palette chip swatches are pure CSS (per-palette gradients), so nothing
  // to wire here. /api/sample stays around for the CLI sample command and
  // for anyone who wants to embed a real procedural hair PNG elsewhere.

  // --- Paste a screenshot from the clipboard ---
  document.addEventListener("paste", (e) => {
    if (!e.clipboardData) return;
    for (const item of e.clipboardData.items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) {
          // Browsers often give pasted screenshots names like "image.png"; that's fine.
          setFiles([f]);
          showInfo("Got it — pasted image is ready.");
          e.preventDefault();
          return;
        }
      }
    }
  });

  // --- Click-to-copy the seed ---
  seedValueEl.title = "click to copy";
  seedValueEl.addEventListener("click", async () => {
    const text = seedValueEl.textContent || "";
    if (!text || text === "—") return;
    try {
      await navigator.clipboard.writeText(text);
      seedValueEl.classList.add("copied");
      setTimeout(() => seedValueEl.classList.remove("copied"), 900);
    } catch (_) {
      // Clipboard API blocked (insecure context / old browser); leave silent.
    }
  });

  // --- Keyboard: Enter to submit, R/S for re-roll ---
  document.addEventListener("keydown", (e) => {
    // Don't hijack typing in the suffix field or other text inputs.
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Enter" && !goBtn.disabled) { goBtn.click(); }
    else if (e.key.toLowerCase() === "r" && !rerollNewBtn.disabled) { rerollNewBtn.click(); }
    else if (e.key.toLowerCase() === "s" && !rerollSameBtn.disabled) { rerollSameBtn.click(); }
  });

  // --- Lightbox (click preview to zoom) -----------------------------------
  const lightbox = document.getElementById("lightbox");
  const lightboxImg = document.getElementById("lightbox-img");
  const lightboxClose = document.getElementById("lightbox-close");

  const lb = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0 };

  function applyLightboxTransform() {
    lightboxImg.style.transform =
      `translate(${lb.tx}px, ${lb.ty}px) scale(${lb.scale})`;
  }

  function resetLightbox() {
    lb.scale = 1; lb.tx = 0; lb.ty = 0;
    applyLightboxTransform();
  }

  function openLightbox(srcUrl) {
    if (!srcUrl) return;
    lightboxImg.src = srcUrl;
    resetLightbox();
    lightbox.hidden = false;
    document.body.style.overflow = "hidden";
  }

  function closeLightbox() {
    lightbox.hidden = true;
    document.body.style.overflow = "";
    lightboxImg.src = "";
  }

  // Click the preview thumbnail to open at full size.
  previewImg.addEventListener("click", () => {
    if (lastPreviewUrl) openLightbox(lastPreviewUrl);
  });

  // Wheel to zoom — anchored toward the cursor for natural zoom-in feel.
  lightbox.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = Math.exp(-e.deltaY * 0.0015);
    const next = Math.max(0.2, Math.min(8, lb.scale * factor));
    // Anchor zoom around the cursor: shift translation so the point
    // under the cursor stays put.
    const rect = lightboxImg.getBoundingClientRect();
    const cx = e.clientX - (rect.left + rect.width / 2);
    const cy = e.clientY - (rect.top + rect.height / 2);
    const ratio = next / lb.scale;
    lb.tx -= cx * (ratio - 1);
    lb.ty -= cy * (ratio - 1);
    lb.scale = next;
    applyLightboxTransform();
  }, { passive: false });

  // Mouse drag-pan.
  lightboxImg.addEventListener("mousedown", (e) => {
    lb.dragging = true;
    lb.sx = e.clientX - lb.tx;
    lb.sy = e.clientY - lb.ty;
    e.preventDefault();
  });
  window.addEventListener("mousemove", (e) => {
    if (!lb.dragging) return;
    lb.tx = e.clientX - lb.sx;
    lb.ty = e.clientY - lb.sy;
    applyLightboxTransform();
  });
  window.addEventListener("mouseup", () => { lb.dragging = false; });

  // Touch: single-finger pan, two-finger pinch-zoom.
  let pinchDist = 0;
  let pinchScale = 1;
  lightboxImg.addEventListener("touchstart", (e) => {
    if (e.touches.length === 2) {
      e.preventDefault();
      pinchDist = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY,
      );
      pinchScale = lb.scale;
    } else if (e.touches.length === 1) {
      lb.dragging = true;
      lb.sx = e.touches[0].clientX - lb.tx;
      lb.sy = e.touches[0].clientY - lb.ty;
    }
  }, { passive: false });
  lightboxImg.addEventListener("touchmove", (e) => {
    e.preventDefault();
    if (e.touches.length === 2) {
      const d = Math.hypot(
        e.touches[0].clientX - e.touches[1].clientX,
        e.touches[0].clientY - e.touches[1].clientY,
      );
      lb.scale = Math.max(0.2, Math.min(8, pinchScale * (d / pinchDist)));
      applyLightboxTransform();
    } else if (e.touches.length === 1 && lb.dragging) {
      lb.tx = e.touches[0].clientX - lb.sx;
      lb.ty = e.touches[0].clientY - lb.sy;
      applyLightboxTransform();
    }
  }, { passive: false });
  lightboxImg.addEventListener("touchend", () => { lb.dragging = false; });

  // Double-click resets zoom + position.
  lightboxImg.addEventListener("dblclick", resetLightbox);

  // Click backdrop closes (but clicks on the image itself don't).
  lightbox.addEventListener("click", (e) => {
    if (e.target === lightbox) closeLightbox();
  });
  lightboxClose.addEventListener("click", closeLightbox);

  // ESC closes (in addition to the existing keydown handler for Enter/R/S).
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !lightbox.hidden) {
      e.stopPropagation();
      closeLightbox();
    }
  });

  // Initial state.
  rerollSameBtn.disabled = true;
  rerollNewBtn.disabled = true;
  downloadBtn.disabled = true;
})();

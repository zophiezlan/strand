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
  const zipSummaryEl = document.getElementById("zip-summary");
  const suffixInput = document.getElementById("suffix");

  const IMAGE_SUFFIXES = new Set([".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"]);
  const SUPPORTED = [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".pdf", ".pptx", ".zip"];
  const MAX_BYTES = 25 * 1024 * 1024;
  const DEFAULT_SUFFIX = "-strand";

  /** Array of File objects currently selected for upload (always >= 0). */
  let currentFiles = [];
  let palette = "dark";
  let intensity = "normal";
  let lastSeed = null;

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
    const skippedNote = skipped > 0 ? ` (${skipped} unsupported skipped)` : "";
    return {
      title: root ? `${root}/ — ${accepted.length} files` : `${accepted.length} files`,
      subtitle: `${sizeKb} — will be returned as a zip${skippedNote}`,
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
    previewEl.hidden = true;
    zipSummaryEl.hidden = true;
    showWorking("Stranding");

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

      const blob = await res.blob();
      const singleFile = currentFiles[0];
      const downloadName = downloadNameFrom(res, singleFile.name);
      const suffix = suffixOf(singleFile.name);

      resultEl.hidden = false;

      const previewSingleImage = currentFiles.length === 1 && IMAGE_SUFFIXES.has(suffix);

      if (isZipResponse) {
        const parts = [];
        if (haired) parts.push(`<strong>${haired}</strong> hairified`);
        if (skipped && Number(skipped) > 0) parts.push(`${skipped} skipped`);
        if (errored && Number(errored) > 0) parts.push(`${errored} errored`);
        zipSummaryEl.innerHTML = parts.join(" · ") +
          ` &nbsp;·&nbsp; see <code>_strand-report.txt</code> inside the zip.`;
        zipSummaryEl.hidden = false;
        showInfo("Done — your hairified zip is downloading.");
      } else if (previewSingleImage) {
        const url = URL.createObjectURL(blob);
        previewImg.src = url;
        previewEl.hidden = false;
        showInfo("Done. Preview below — file is also downloading.");
      } else {
        showInfo("Done — your hairified file is downloading.");
      }

      triggerDownload(blob, downloadName);
    } catch (err) {
      showError(`Network error: ${err.message || err}`);
    } finally {
      goBtn.disabled = false;
      rerollSameBtn.disabled = lastSeed == null;
      rerollNewBtn.disabled = false;
    }
  }

  goBtn.addEventListener("click", () => submit({ reuseSeed: false }));
  rerollSameBtn.addEventListener("click", () => submit({ reuseSeed: true }));
  rerollNewBtn.addEventListener("click", () => submit({ reuseSeed: false }));

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

  // --- Palette chip previews ---
  // Each palette chip gets a small inline hair PNG fetched from /api/sample.
  // Uses a fixed seed per palette so the chip preview is stable across reloads
  // (caches well, doesn't flicker). Falls back silently if /api/sample is
  // unreachable (e.g. when the HTML is opened from disk via a preview pane).
  const PALETTE_SEEDS = {
    dark: 1, brown: 2, blonde: 3, red: 4, grey: 5, white: 6, mixed: 7,
  };
  for (const chip of document.querySelectorAll(".palette-chip")) {
    const value = chip.dataset.value;
    const sample = chip.querySelector(".chip-sample");
    if (!sample) continue;
    const seed = PALETTE_SEEDS[value] ?? 1;
    const url = `/api/sample?palette=${encodeURIComponent(value)}&seed=${seed}`;
    // Probe by setting a background-image; if it 404s the chip just keeps its
    // empty thumbnail. No console noise either way.
    const probe = new Image();
    probe.onload = () => { sample.style.backgroundImage = `url("${url}")`; };
    probe.src = url;
  }

  // --- Paste a screenshot from the clipboard ---
  document.addEventListener("paste", (e) => {
    if (!e.clipboardData) return;
    for (const item of e.clipboardData.items) {
      if (item.kind === "file") {
        const f = item.getAsFile();
        if (f) {
          // Browsers often give pasted screenshots names like "image.png"; that's fine.
          setFiles([f]);
          showInfo("Got it — pasted clipboard image is ready.");
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

  // Initial state.
  rerollSameBtn.disabled = true;
  rerollNewBtn.disabled = true;
})();

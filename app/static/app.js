(() => {
  const dropZone = document.getElementById("drop");
  const fileInput = document.getElementById("file");
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
  const DEFAULT_SUFFIX = "-haired";

  let currentFile = null;
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

  // --- File picker / drop zone ---
  dropZone.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files[0]) setFile(fileInput.files[0]);
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
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setFile(e.dataTransfer.files[0]);
    }
  });

  function suffixOf(name) {
    const i = name.lastIndexOf(".");
    return i < 0 ? "" : name.slice(i).toLowerCase();
  }

  function setFile(f) {
    if (f.size > MAX_BYTES) {
      showError(`File is ${(f.size / (1024 * 1024)).toFixed(1)} MB — limit is 25 MB.`);
      return;
    }
    const suffix = suffixOf(f.name);
    if (!SUPPORTED.includes(suffix)) {
      showError(`Sorry, ${suffix || "that"} isn't supported. Try ${SUPPORTED.join(", ")}.`);
      return;
    }
    currentFile = f;
    lastSeed = null;
    dropZone.classList.add("has-file");
    dropZone.querySelector(".drop-text strong").textContent = f.name;
    dropZone.querySelector(".drop-text span").textContent =
      `${(f.size / 1024).toFixed(0)} KB — ready`;
    goBtn.disabled = false;
    hideStatus();
    resultEl.hidden = true;
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
    if (!currentFile) return;
    goBtn.disabled = true;
    rerollSameBtn.disabled = true;
    rerollNewBtn.disabled = true;
    previewEl.hidden = true;
    zipSummaryEl.hidden = true;
    showWorking("Hairifying");

    try {
      const form = new FormData();
      form.append("file", currentFile);
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

      const res = await fetch("/hairify", { method: "POST", body: form });

      if (!res.ok) {
        let detail = `Server returned ${res.status}.`;
        try {
          const body = await res.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) { /* not JSON; keep default */ }
        showError(detail);
        return;
      }

      const echoedSeed = res.headers.get("X-Hairify-Seed");
      if (echoedSeed) {
        lastSeed = echoedSeed;
        seedValueEl.textContent = echoedSeed;
      }

      const haired = res.headers.get("X-Hairify-Haired");
      const skipped = res.headers.get("X-Hairify-Skipped");
      const errored = res.headers.get("X-Hairify-Errored");
      const isZipResponse = haired != null || skipped != null;

      const blob = await res.blob();
      const downloadName = downloadNameFrom(res, currentFile.name);

      const suffix = suffixOf(currentFile.name);

      resultEl.hidden = false;

      if (isZipResponse) {
        const parts = [];
        if (haired) parts.push(`<strong>${haired}</strong> hairified`);
        if (skipped && Number(skipped) > 0) parts.push(`${skipped} skipped`);
        if (errored && Number(errored) > 0) parts.push(`${errored} errored`);
        zipSummaryEl.innerHTML = parts.join(" · ") +
          ` &nbsp;·&nbsp; see <code>_hairify-report.txt</code> inside the zip.`;
        zipSummaryEl.hidden = false;
        showInfo("Done — your hairified zip is downloading.");
      } else if (IMAGE_SUFFIXES.has(suffix)) {
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
    return i < 0 ? `${fallback}-haired` : `${fallback.slice(0, i)}-haired${fallback.slice(i)}`;
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

  // Initial state.
  rerollSameBtn.disabled = true;
  rerollNewBtn.disabled = true;
})();

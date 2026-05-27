import * as engine from "/pyodide_engine.js";

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
  const downloadBtn = document.getElementById("download");
  const compareBtn = document.getElementById("compare");
  const zipSummaryEl = document.getElementById("zip-summary");
  const statsEl = document.getElementById("stats");
  const statsLineEl = document.getElementById("stats-line");
  const suffixInput = document.getElementById("suffix");
  const useServerCheckbox = document.getElementById("use-server");
  const settingsBtn = document.getElementById("settings-btn");
  const settingsModal = document.getElementById("settings-modal");
  const themeRadios = document.querySelectorAll('input[name="theme"]');

  // --- Settings: theme + server-mode persistence -------------------------
  // Theme is applied pre-paint by an inline script in index.html so we don't
  // flash light on first load. This block syncs the radio UI to the persisted
  // value, keeps "system" mode reactive to OS-level changes, and persists
  // user choices for both controls.
  const THEME_KEY = "strand-theme";
  const SERVER_KEY = "strand-use-server";
  const themeMedia = window.matchMedia("(prefers-color-scheme: dark)");

  function applyTheme(setting) {
    const dark =
      setting === "dark" || (setting === "system" && themeMedia.matches);
    document.documentElement.setAttribute(
      "data-theme",
      dark ? "dark" : "light",
    );
  }

  let themeSetting = localStorage.getItem(THEME_KEY) || "system";
  for (const r of themeRadios) r.checked = r.value === themeSetting;
  themeMedia.addEventListener("change", () => {
    if (themeSetting === "system") applyTheme("system");
  });
  for (const r of themeRadios) {
    r.addEventListener("change", () => {
      if (!r.checked) return;
      themeSetting = r.value;
      localStorage.setItem(THEME_KEY, themeSetting);
      applyTheme(themeSetting);
    });
  }

  // Server-mode: default off (per spec). Persisted across reloads if the user
  // explicitly opts in; the HTML default of "unchecked" wins on first visit.
  useServerCheckbox.checked = localStorage.getItem(SERVER_KEY) === "1";
  useServerCheckbox.addEventListener("change", () => {
    if (useServerCheckbox.checked) localStorage.setItem(SERVER_KEY, "1");
    else localStorage.removeItem(SERVER_KEY);
  });

  settingsBtn.addEventListener("click", () => settingsModal.showModal());

  const IMAGE_SUFFIXES = new Set([
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
  ]);
  const SUPPORTED = [
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".pdf",
    ".pptx",
    ".zip",
  ];
  const MAX_BYTES = 25 * 1024 * 1024;

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
  let lastPreviewUrl = null; // separate object URL for inline preview img
  /** Object URL for the original (un-haired) upload, used by the hold-to-
      peek Compare button. Only populated for single-image responses. */
  let lastOriginalUrl = null;

  // --- Chip selectors ---
  // The .controls section carries data-palette so CSS can tint the selected
  // pills (both the palette chip and the density chip) with the active hair
  // colour. Initial value matches the default palette declared above.
  const controlsEl = document.querySelector(".controls");
  controlsEl.dataset.palette = palette;
  for (const group of ["palette", "intensity"]) {
    const groupEl = document.getElementById(group);
    groupEl.addEventListener("click", (e) => {
      const chip = e.target.closest(".chip");
      if (!chip) return;
      for (const c of groupEl.querySelectorAll(".chip")) {
        c.classList.toggle("selected", c === chip);
        c.setAttribute("aria-checked", c === chip ? "true" : "false");
      }
      if (group === "palette") {
        palette = chip.dataset.value;
        controlsEl.dataset.palette = palette;
      } else {
        intensity = chip.dataset.value;
      }
    });
  }

  // --- File / folder pickers, drop zone ---
  // Click on the drop zone opens the file picker, unless:
  //   • the click was on a link inside it (the "pick a folder" link has its
  //     own handler and triggers the folder picker), OR
  //   • the click bubbled up from one of the hidden <input> elements — those
  //     are triggered programmatically (e.g. from the folder-link handler)
  //     and we'd otherwise queue a second file picker on top of the one we
  //     just opened on the user's behalf.
  dropZone.addEventListener("click", (e) => {
    if (e.target.closest("a, input")) return;
    fileInput.click();
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files && fileInput.files.length)
      setFiles(Array.from(fileInput.files));
  });
  pickFolderLink.addEventListener("click", (e) => {
    e.preventDefault();
    folderInput.click();
  });
  folderInput.addEventListener("change", () => {
    if (folderInput.files && folderInput.files.length)
      setFiles(Array.from(folderInput.files));
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

  // Kick off the in-browser engine boot as soon as the user shows intent
  // (first file drop / pick / paste). Boots once; subsequent calls reuse the
  // cached interpreter. We don't await it here — by the time the user clicks
  // "Strand it" it's usually ready, and if not, submit() shows the boot status.
  // Skipped when the user has opted into the server fallback, to avoid the
  // ~10 MB download for users who don't need it.
  let _engineWarmed = false;
  function warmEngine() {
    if (_engineWarmed) return;
    if (useServerCheckbox.checked) return;
    _engineWarmed = true;
    engine.prefetch().catch(() => {
      /* surface only when the user actually runs */
    });
  }
  // If the user ticks the server checkbox we don't need to warm; if they
  // later untick, kick the boot off then.
  useServerCheckbox.addEventListener("change", () => {
    if (!useServerCheckbox.checked && currentFiles.length) warmEngine();
  });

  async function runViaEngine({ palette, intensity, seed, nameSuffix }) {
    // Bytes for the engine. Reading concurrently is faster on multi-file.
    const fileBuffers = await Promise.all(
      currentFiles.map(async (f) => ({
        name: fileRelPath(f),
        bytes: new Uint8Array(await f.arrayBuffer()),
      })),
    );
    const kickoff = kickoffMessage(currentFiles, intensity);
    return engine.run({
      files: fileBuffers,
      palette,
      intensity,
      seed,
      nameSuffix,
      onStatus: (msg) =>
        showWorking(msg === "Ready" || msg === "Adding hair" ? kickoff : msg),
    });
  }

  // The Pyodide engine runs in a worker so the page stays responsive, but
  // big decks at wild densities can still take a minute or two of CPU. If
  // the user has signed up for that combination, set expectations up front
  // so the spinner doesn't feel mysterious.
  function kickoffMessage(files, intensity) {
    const wild =
      intensity === "hirsute" ||
      intensity === "werewolf" ||
      intensity === "cousin-itt";
    const heavy = files.some((f) => {
      const s = suffixOf(f.name);
      return s === ".pdf" || s === ".pptx" || s === ".zip";
    });
    if (wild && heavy)
      return "Adding hair — a dense deck takes a minute or two";
    if (wild) return "Adding hair — this one's a lot";
    if (heavy && files.length > 1) return "Adding hair across your files";
    if (heavy) return "Adding hair through the pages";
    return "Adding hair";
  }

  async function runViaServer({ palette, intensity, seed, nameSuffix }) {
    const form = new FormData();
    for (const f of currentFiles) {
      form.append("file", f, fileRelPath(f));
    }
    form.append("palette", palette);
    form.append("intensity", intensity);
    if (seed != null) form.append("seed", String(seed));
    if (nameSuffix != null) form.append("name_suffix", nameSuffix);

    const res = await fetch("/strand", { method: "POST", body: form });
    if (!res.ok) {
      let detail = `Server returned ${res.status}.`;
      try {
        const body = await res.json();
        if (body && body.detail) detail = body.detail;
      } catch (_) {
        /* not JSON */
      }
      throw new Error(detail);
    }
    const ab = await res.arrayBuffer();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = cd.match(/filename="?([^"]+)"?/);
    const haired = res.headers.get("X-Strand-Haired");
    const skipped = res.headers.get("X-Strand-Skipped");
    const errored = res.headers.get("X-Strand-Errored");
    const statsHeader = res.headers.get("X-Strand-Stats");
    return {
      bytes: new Uint8Array(ab),
      name: m ? m[1] : currentFiles[0].name,
      contentType:
        res.headers.get("Content-Type") || "application/octet-stream",
      seed: Number(res.headers.get("X-Strand-Seed")) || null,
      stats: statsHeader ? JSON.parse(statsHeader) : null,
      haired: haired != null ? Number(haired) : null,
      skipped: skipped != null ? Number(skipped) : null,
      errored: errored != null ? Number(errored) : null,
    };
  }

  function setFiles(files) {
    // Filter to supported types; surface a friendly message if everything got dropped.
    const accepted = files.filter((f) => SUPPORTED.includes(suffixOf(f.name)));
    if (!accepted.length) {
      const tried = files
        .map((f) => suffixOf(f.name) || "(no extension)")
        .join(", ");
      showError(
        `None of those are supported (${tried}). Try ${SUPPORTED.join(", ")}.`,
      );
      return;
    }

    const total = accepted.reduce((s, f) => s + f.size, 0);
    if (total > MAX_BYTES) {
      showError(
        `Selection is ${(total / (1024 * 1024)).toFixed(1)} MB — limit is 25 MB total.`,
      );
      return;
    }

    currentFiles = accepted;
    // Keep lastSeed across uploads — the Mix code is the seed for the *next*
    // run, and "Do that one again" should let you apply it to a freshly
    // dropped file. Clearing it here was what made the button feel redundant.
    dropZone.classList.add("has-file");
    warmEngine();

    const summary = describeSelection(accepted, total, files.length);
    dropZone.querySelector(".drop-text strong").textContent = summary.title;
    dropZone.querySelector(".drop-text span").textContent = summary.subtitle;

    goBtn.disabled = false;
    hideStatus();

    // The preview / stats / download from the prior run no longer match the
    // new upload. Hide the parts that lie, keep the result panel itself
    // visible so the Mix code + "Do that one again" stay reachable.
    previewEl.hidden = true;
    zipSummaryEl.hidden = true;
    statsEl.hidden = true;
    compareBtn.hidden = true;
    downloadBtn.disabled = true;
    lastResultBlob = null;
    lastResultName = null;
    if (lastPreviewUrl) {
      URL.revokeObjectURL(lastPreviewUrl);
      lastPreviewUrl = null;
    }
    if (lastOriginalUrl) {
      URL.revokeObjectURL(lastOriginalUrl);
      lastOriginalUrl = null;
    }
    resultEl.hidden = lastSeed == null;
    rerollSameBtn.disabled = lastSeed == null;
  }

  function describeSelection(accepted, totalBytes, originalCount) {
    const sizeKb = `${(totalBytes / 1024).toFixed(0)} KB`;
    if (accepted.length === 1) {
      return { title: accepted[0].name, subtitle: `${sizeKb} — ready` };
    }
    // Try to surface a common folder root when the user picked a directory.
    const rels = accepted.map(fileRelPath);
    const roots = new Set(rels.map((p) => p.split("/")[0]));
    const root =
      roots.size === 1 && rels.every((p) => p.includes("/"))
        ? roots.values().next().value
        : null;
    const skipped = originalCount - accepted.length;
    const skippedNote =
      skipped > 0 ? ` (${skipped} we can't handle will stay out)` : "";
    return {
      title: root
        ? `${root}/ — ${accepted.length} files`
        : `${accepted.length} files`,
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
    // Tag every "working" message with where the work is happening. The
    // server-mode toggle hides in settings, so without this indicator a user
    // who'd left it on weeks ago would have no on-screen clue that their file
    // is being uploaded rather than processed locally.
    statusEl.innerHTML = "";
    statusEl.append(document.createTextNode(msg));
    const runtime = document.createElement("span");
    runtime.className = "status-runtime";
    runtime.textContent = useServerCheckbox.checked
      ? "on the server"
      : "in your browser";
    statusEl.append(runtime);
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
    downloadBtn.disabled = true;
    previewEl.hidden = true;
    zipSummaryEl.hidden = true;
    statsEl.hidden = true;
    compareBtn.hidden = true;
    // Discard any prior result + preview URL before starting fresh.
    if (lastPreviewUrl) {
      URL.revokeObjectURL(lastPreviewUrl);
      lastPreviewUrl = null;
    }
    if (lastOriginalUrl) {
      URL.revokeObjectURL(lastOriginalUrl);
      lastOriginalUrl = null;
    }
    lastResultBlob = null;
    lastResultName = null;
    showWorking(kickoffMessage(currentFiles, intensity));

    try {
      // Blank input → no suffix (keep original filename). Anything else is
      // used verbatim. Matches the prior server-side behaviour.
      const suffixValue = (suffixInput.value || "").trim();
      const nameSuffix = suffixValue === "" ? "" : suffixValue;
      const seedForRun = reuseSeed && lastSeed != null ? lastSeed : null;

      const result = useServerCheckbox.checked
        ? await runViaServer({
            palette,
            intensity,
            seed: seedForRun,
            nameSuffix,
          })
        : await runViaEngine({
            palette,
            intensity,
            seed: seedForRun,
            nameSuffix,
          });

      if (result.seed != null) {
        lastSeed = String(result.seed);
        seedValueEl.textContent = lastSeed;
      }

      const haired = result.haired;
      const skipped = result.skipped;
      const errored = result.errored;
      const isZipResponse = haired != null || skipped != null;

      // Stats panel — hidden by default, opens on click for nerds who want
      // to know which morphologies got drawn and across how many pages.
      renderStats(result.stats ? JSON.stringify(result.stats) : null);

      const blob = new Blob([result.bytes], { type: result.contentType });
      const singleFile = currentFiles[0];
      const downloadName = result.name;
      const suffix = suffixOf(singleFile.name);

      // Stash for the explicit Download button — the page no longer
      // auto-fires the save dialog. The user looks at the preview first.
      lastResultBlob = blob;
      lastResultName = downloadName;

      resultEl.hidden = false;

      const previewSingleImage =
        currentFiles.length === 1 && IMAGE_SUFFIXES.has(suffix);
      const previewPdf =
        currentFiles.length === 1 &&
        suffix === ".pdf" &&
        result.previewAfter &&
        result.previewBefore;

      if (isZipResponse) {
        const parts = [];
        if (haired) parts.push(`<strong>${haired}</strong> got hair`);
        if (skipped && Number(skipped) > 0) parts.push(`${skipped} left alone`);
        if (errored && Number(errored) > 0)
          parts.push(`${errored} had trouble`);
        zipSummaryEl.innerHTML =
          parts.join(" · ") +
          ` &nbsp;·&nbsp; there's a summary inside the zip if you want the details.`;
        zipSummaryEl.hidden = false;
        showInfo("Done — your zip's ready.");
      } else if (previewSingleImage) {
        lastPreviewUrl = URL.createObjectURL(blob);
        // Stash an object URL for the original upload so the Compare button
        // can flip the preview back to "before" while the user holds it.
        lastOriginalUrl = URL.createObjectURL(singleFile);
        previewImg.src = lastPreviewUrl;
        previewEl.hidden = false;
        compareBtn.hidden = false;
        showInfo("Done. How does it look?");
      } else if (previewPdf) {
        // PDFs come back with a before/after pair of PNGs — one of the page
        // that picked up the first round of hairs. Same compare flow as
        // images, just sourced from the engine rather than the original file.
        lastPreviewUrl = URL.createObjectURL(
          new Blob([result.previewAfter], { type: "image/png" }),
        );
        lastOriginalUrl = URL.createObjectURL(
          new Blob([result.previewBefore], { type: "image/png" }),
        );
        previewImg.src = lastPreviewUrl;
        previewEl.hidden = false;
        compareBtn.hidden = false;
        showInfo("Done. How does it look?");
      } else {
        showInfo("Done — ready when you are.");
      }
    } catch (err) {
      showError(err.message || String(err));
    } finally {
      goBtn.disabled = false;
      rerollSameBtn.disabled = lastSeed == null;
      downloadBtn.disabled = lastResultBlob == null;
    }
  }

  goBtn.addEventListener("click", () => submit({ reuseSeed: false }));
  rerollSameBtn.addEventListener("click", () => submit({ reuseSeed: true }));
  downloadBtn.addEventListener("click", () => {
    if (lastResultBlob && lastResultName) {
      triggerDownload(lastResultBlob, lastResultName);
    }
  });

  // Hold-to-peek compare: while pointer is down, swap to the original; on
  // release (or if the pointer leaves the button or window blurs), swap back.
  // Single pointerdown/up handler covers mouse, touch, and pen.
  function startPeek(e) {
    if (compareBtn.hidden || !lastOriginalUrl || !lastPreviewUrl) return;
    e.preventDefault();
    previewImg.src = lastOriginalUrl;
    compareBtn.classList.add("peeking");
    compareBtn.textContent = "Showing original";
  }
  function endPeek() {
    if (!compareBtn.classList.contains("peeking")) return;
    previewImg.src = lastPreviewUrl;
    compareBtn.classList.remove("peeking");
    compareBtn.textContent = "Hold to compare";
  }
  compareBtn.addEventListener("pointerdown", startPeek);
  compareBtn.addEventListener("pointerup", endPeek);
  compareBtn.addEventListener("pointerleave", endPeek);
  compareBtn.addEventListener("pointercancel", endPeek);
  window.addEventListener("blur", endPeek);
  // Keyboard accessibility — Space/Enter on a focused button mimic mouse.
  compareBtn.addEventListener("keydown", (e) => {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      startPeek(e);
    }
  });
  compareBtn.addEventListener("keyup", (e) => {
    if (e.key === " " || e.key === "Enter") {
      endPeek();
    }
  });

  function renderStats(headerValue) {
    if (!headerValue) {
      statsEl.hidden = true;
      return;
    }
    let s;
    try {
      s = JSON.parse(headerValue);
    } catch {
      statsEl.hidden = true;
      return;
    }

    const total = s.hairs || 0;
    if (total === 0) {
      statsEl.hidden = true;
      return;
    }

    const pages = s.pages_touched || 0;
    const clusters = s.clusters || 0;
    const morphs = entriesByCount(s.morphologies || {});
    const palettes = entriesByCount(s.palettes || {});
    const lengths = s.hair_lengths_cm || [];
    const contentHits = s.content_hits || 0;
    const substrate = s.substrate;

    const lines = [];

    // The stats panel is meant to read like a quick recap of what happened,
    // not a table of numbers. Each line is one beat: how many landed, what
    // kinds, what colour, how long, how many found content, and the canvas
    // they were laid on. The voice should match the rest of the app — light,
    // a little theatrical, never clinical.

    // Line 1: scene-set with the totals.
    if (total === 1) {
      lines.push("Just the one hair.");
    } else {
      let s = `${total} hairs`;
      if (pages > 1) s += ` across ${pages} pages`;
      if (clusters > 0) {
        s +=
          clusters === 1 ? ", all huddled together" : `, in ${clusters} clumps`;
      }
      lines.push(s + ".");
    }

    // Line 2: morphology — pick out the dominant type, then "with a, b, c".
    if (morphs.length === 1 && morphs[0][1] === total) {
      if (total === 1) {
        lines.push(`A single ${morphs[0][0]}.`);
      } else {
        lines.push(`All ${pluralize(morphs[0][0], total)}.`);
      }
    } else if (morphs.length > 1) {
      const [headName, headN] = morphs[0];
      const rest = morphs
        .slice(1)
        .map(([name, n]) => `${n} ${pluralize(name, n)}`);
      lines.push(
        `Mostly ${pluralize(headName, headN)} (${headN}), with ${joinList(rest)}.`,
      );
    }

    // Line 3: colour.
    if (palettes.length === 1) {
      lines.push(
        total === 1
          ? `One ${palettes[0][0]} strand.`
          : `All ${palettes[0][0]}.`,
      );
    } else if (palettes.length > 1) {
      const parts = palettes.map(([name, n]) => `${n} ${name}`);
      lines.push(`A mix: ${joinList(parts)}.`);
    }

    // Line 4: physical hair lengths. Concrete proof the cm-sizing is real.
    if (lengths.length === 1) {
      lines.push(`${lengths[0].toFixed(1)} cm long.`);
    } else if (lengths.length > 1) {
      const sorted = [...lengths].sort((a, b) => a - b);
      const min = sorted[0];
      const max = sorted[sorted.length - 1];
      const median = sorted[Math.floor(sorted.length / 2)];
      lines.push(
        `${min.toFixed(1)} to ${max.toFixed(1)} cm long, median ${median.toFixed(1)}.`,
      );
    }

    // Line 5: aim. Frame it as the story — how many found something to land
    // on vs. how many drifted into the margin.
    if (total > 1 && contentHits > 0) {
      const missed = total - contentHits;
      if (missed === 0) {
        lines.push(`Every one landed on something.`);
      } else if (missed === 1) {
        lines.push(
          `${contentHits} of ${total} landed on content; one drifted into the margin.`,
        );
      } else {
        lines.push(
          `${contentHits} of ${total} landed on content; the other ${missed} hit blank space.`,
        );
      }
    }

    // Line 6: canvas — physical dimensions of the file we just haired.
    if (substrate) {
      const nat = `${formatNative(substrate)}`;
      const cm = `${substrate.width_cm} × ${substrate.height_cm} cm`;
      const depth = substrate.page_count
        ? `, ${substrate.page_count} pages deep`
        : substrate.slide_count
          ? `, ${substrate.slide_count} slides deep`
          : "";
      lines.push(`All this on a ${nat} canvas (≈${cm})${depth}.`);
    }

    statsLineEl.innerHTML = lines
      .map((l) => `<span class="stats-line">${escapeHtml(l)}</span>`)
      .join("");
    statsEl.hidden = false;
  }

  function formatNative(substrate) {
    const u = substrate.native_unit;
    if (u === "px") {
      const dpi = substrate.dpi ? ` @ ${substrate.dpi} dpi` : "";
      return `${substrate.width_native} × ${substrate.height_native} px${dpi}`;
    }
    if (u === "pt") {
      return `${substrate.width_native} × ${substrate.height_native} pt`;
    }
    if (u === "EMU") {
      // EMU values are huge — show as inches for human readability.
      const wIn = (substrate.width_native / 914400).toFixed(2);
      const hIn = (substrate.height_native / 914400).toFixed(2);
      return `${wIn} × ${hIn} in`;
    }
    return `${substrate.width_native} × ${substrate.height_native} ${u}`;
  }

  function entriesByCount(obj) {
    return Object.entries(obj)
      .filter(([, n]) => n > 0)
      .sort((a, b) => b[1] - a[1]);
  }

  function pluralize(word, n) {
    if (n === 1) return word;
    // Words ending in s/x/z/sh/ch need "es" — covers "eyelash" → "eyelashes".
    if (/(s|x|z|sh|ch)$/.test(word)) return word + "es";
    return word + "s";
  }

  // Comma-separated list with an Oxford "and" before the last item.
  // ["a"] → "a"; ["a","b"] → "a and b"; ["a","b","c"] → "a, b, and c".
  function joinList(items) {
    if (items.length === 0) return "";
    if (items.length === 1) return items[0];
    if (items.length === 2) return `${items[0]} and ${items[1]}`;
    return `${items.slice(0, -1).join(", ")}, and ${items[items.length - 1]}`;
  }

  function escapeHtml(s) {
    return s.replace(
      /[&<>"']/g,
      (ch) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[ch],
    );
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
    if (
      e.target instanceof HTMLInputElement ||
      e.target instanceof HTMLTextAreaElement
    )
      return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "Enter" && !goBtn.disabled) {
      goBtn.click();
    }
    // "R" picks a fresh seed — same effect as Strand it, kept as a shortcut.
    else if (e.key.toLowerCase() === "r" && !goBtn.disabled) {
      goBtn.click();
    } else if (e.key.toLowerCase() === "s" && !rerollSameBtn.disabled) {
      rerollSameBtn.click();
    }
  });

  // --- Lightbox (click preview to zoom) -----------------------------------
  const lightbox = document.getElementById("lightbox");
  const lightboxImg = document.getElementById("lightbox-img");
  const lightboxClose = document.getElementById("lightbox-close");

  const lb = { scale: 1, tx: 0, ty: 0, dragging: false, sx: 0, sy: 0 };

  function applyLightboxTransform() {
    lightboxImg.style.transform = `translate(${lb.tx}px, ${lb.ty}px) scale(${lb.scale})`;
  }

  function resetLightbox() {
    lb.scale = 1;
    lb.tx = 0;
    lb.ty = 0;
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
  lightbox.addEventListener(
    "wheel",
    (e) => {
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
    },
    { passive: false },
  );

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
  window.addEventListener("mouseup", () => {
    lb.dragging = false;
  });

  // Touch: single-finger pan, two-finger pinch-zoom.
  let pinchDist = 0;
  let pinchScale = 1;
  lightboxImg.addEventListener(
    "touchstart",
    (e) => {
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
    },
    { passive: false },
  );
  lightboxImg.addEventListener(
    "touchmove",
    (e) => {
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
    },
    { passive: false },
  );
  lightboxImg.addEventListener("touchend", () => {
    lb.dragging = false;
  });

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
  downloadBtn.disabled = true;
})();

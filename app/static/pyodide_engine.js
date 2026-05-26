// Strand — Pyodide engine wrapper.
//
// Lazy-loads Pyodide on first call, installs Pillow + PyMuPDF + python-pptx,
// fetches app/core.py from /core.py, and exposes a single async `run(...)`
// that mirrors the FastAPI `POST /strand` route — same dispatch logic
// (single file → strand_bytes; zip or multi-file → strand_zip_bytes), same
// response shape (out bytes, download name, content type, seed, stats,
// optional haired/skipped/errored counts).
//
// The runtime is cached in-module so subsequent calls are instant.

const PYODIDE_VERSION = "0.29.4";
const PYODIDE_INDEX_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;

// Python glue that owns the file-shape decisions. Same control flow as the
// `strand()` handler in app/main.py — kept here so the JS side never has to
// touch zip bundling, suffix application, or the unsupported-type rules.
const ENGINE_PY = `
from __future__ import annotations
import io, zipfile
from app import core

_OUTPUT_CONTENT_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
    ".webp": "image/webp",
    ".pdf":  "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip":  "application/zip",
}

class StrandError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg); self.code = code

def _bundle(items):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            safe = name.replace("\\\\", "/").lstrip("/")
            if not safe:
                continue
            z.writestr(safe, data)
    return buf.getvalue()

def _common_root(names):
    roots = set()
    for n in names:
        norm = n.replace("\\\\", "/")
        if "/" not in norm:
            return None
        roots.add(norm.split("/", 1)[0])
    return roots.pop() if len(roots) == 1 else None

def run(items, *, palette, intensity, seed=None, name_suffix="-strand"):
    """items: list[(name, bytes)]. Returns dict shaped for the JS caller."""
    if not items:
        raise StrandError("No file uploaded.", 400)

    opts = core.options_from_ui(palette=palette, intensity=intensity, seed=seed)
    suffix = name_suffix if name_suffix is not None else "-strand"

    single = len(items) == 1
    single_name, single_data = items[0] if single else (None, None)
    single_ext = core._suffix_of(single_name) if single else None
    single_is_zip = single and single_ext in core.ZIP_SUFFIXES

    haired = skipped = errored = None

    if single and not single_is_zip:
        if single_ext not in core.SUPPORTED_SUFFIXES:
            allowed = ", ".join(sorted(core.SUPPORTED_INCLUDING_ZIP))
            raise StrandError(
                f"Unsupported file type: {single_ext or '(none)'}. Supported: {allowed}.",
                415,
            )
        out, stats = core.strand_bytes(single_data, single_name, opts)
        out_ext = single_ext
        out_name = core._apply_suffix(single_name, suffix)
    else:
        if single_is_zip:
            payload = single_data
            base = single_name
        else:
            payload = _bundle(items)
            root = _common_root([n for n, _ in items])
            base = (root or "files") + ".zip"
        out, report = core.strand_zip_bytes(payload, opts, name_suffix=suffix)
        stats = report.get("stats")
        haired = report["haired_count"]
        skipped = report["skipped_count"]
        errored = report["error_count"]
        out_ext = ".zip"
        out_name = core._apply_suffix(base, suffix)

    return {
        "out_bytes": out,
        "out_name": out_name,
        "content_type": _OUTPUT_CONTENT_TYPES.get(out_ext, "application/octet-stream"),
        "seed": opts.seed,
        "stats": stats,
        "haired": haired,
        "skipped": skipped,
        "errored": errored,
    }

def sample_hair_png(palette, morphology=None, seed=None):
    """Return one hair PNG on transparent background. Powers the chip previews."""
    import random
    rng = random.Random(seed) if seed is not None else random.Random()
    pal = (palette or "").lower().strip() or "dark"
    if pal not in core.PALETTE_NAMES:
        raise StrandError(f"unknown palette: {palette!r}", 400)
    morph = (morphology or "").lower().strip() or None
    if morph:
        if morph not in core.MORPHOLOGIES:
            raise StrandError(f"unknown morphology: {morph!r}", 400)
        img = core.MORPHOLOGIES[morph](rng, palette=pal)
    else:
        img = core.generate_hair(rng, palette=pal)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()
`;

let _bootPromise = null;
let _pyodide = null;

async function _injectPyodideScript() {
  if (window.loadPyodide) return;
  await new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = PYODIDE_INDEX_URL + "pyodide.js";
    s.onload = resolve;
    s.onerror = () => reject(new Error("Failed to load Pyodide runtime."));
    document.head.appendChild(s);
  });
}

async function _boot(onStatus) {
  onStatus("Loading engine (one-time ~10 MB)…");
  await _injectPyodideScript();
  const py = await window.loadPyodide({ indexURL: PYODIDE_INDEX_URL });

  onStatus("Loading image libraries…");
  await py.loadPackage(["Pillow", "pymupdf", "lxml", "micropip"]);

  onStatus("Installing python-pptx…");
  await py.runPythonAsync(`
import micropip
await micropip.install("python-pptx")
`);

  onStatus("Fetching renderer…");
  const coreSrc = await (await fetch("/core.py", { cache: "no-cache" })).text();
  py.FS.mkdir("/app");
  py.FS.writeFile("/app/__init__.py", "");
  py.FS.writeFile("/app/core.py", coreSrc);
  py.FS.writeFile("/app/engine.py", ENGINE_PY);

  await py.runPythonAsync(`
import sys
if "/" not in sys.path: sys.path.insert(0, "/")
from app import engine as _strand_engine  # warm import
`);

  onStatus("Ready");
  _pyodide = py;
  return py;
}

/** Kick off the boot now (idempotent). Returns a promise that resolves to the
 *  Pyodide instance. The first caller drives the boot; subsequent callers
 *  attach to the same promise. */
export function prefetch(onStatus = () => {}) {
  if (_pyodide) return Promise.resolve(_pyodide);
  if (!_bootPromise) _bootPromise = _boot(onStatus);
  return _bootPromise;
}

/** True once Pyodide has finished booting. */
export function isReady() {
  return _pyodide !== null;
}

/**
 * Run the strand pipeline in-browser.
 *
 * @param {object} args
 * @param {Array<{name: string, bytes: ArrayBuffer|Uint8Array}>} args.files
 * @param {string} args.palette
 * @param {string} args.intensity
 * @param {number|string|null} [args.seed]
 * @param {string|null} [args.nameSuffix]  null/undefined = default ("-strand"); "" = keep original name
 * @param {(msg: string) => void} [args.onStatus]
 * @returns {Promise<{bytes: Uint8Array, name: string, contentType: string, seed: number, stats: object|null, haired: number|null, skipped: number|null, errored: number|null}>}
 */
export async function run({ files, palette, intensity, seed = null, nameSuffix = null, onStatus = () => {} }) {
  const py = await prefetch(onStatus);

  // Normalize: each file becomes a (name, Uint8Array) pair on the Python side.
  // Uint8Array .to_py() in Pyodide hands you a memoryview that bytes() accepts.
  const items = files.map((f) => ({
    name: f.name,
    bytes: f.bytes instanceof Uint8Array ? f.bytes : new Uint8Array(f.bytes),
  }));

  // Pyodide's toPy() leaves nested JS `null` as `JsNull` (not Python None),
  // which breaks `is None` checks downstream. Simplest workaround: only
  // include keys whose values are present, and let Python `.get()` supply None.
  const argsObj = { items, palette, intensity };
  if (seed !== null && seed !== undefined) argsObj.seed = seed;
  if (nameSuffix !== null && nameSuffix !== undefined) argsObj.name_suffix = nameSuffix;
  const argsProxy = py.toPy(argsObj);
  py.globals.set("_args", argsProxy);

  const result = await py.runPythonAsync(`
from app.engine import run as _run, StrandError
try:
    _items = [(it["name"], bytes(it["bytes"])) for it in _args["items"]]
    _seed_v = _args.get("seed")
    if _seed_v == "" or _seed_v is None:
        _seed_v = None
    else:
        _seed_v = int(_seed_v)
    _suffix_v = _args.get("name_suffix")
    if _suffix_v is not None:
        _suffix_v = str(_suffix_v)
    _result = _run(_items,
                   palette=str(_args["palette"]),
                   intensity=str(_args["intensity"]),
                   seed=_seed_v,
                   name_suffix=_suffix_v)
    _result["ok"] = True
except StrandError as e:
    _result = {"ok": False, "error": str(e), "code": e.code}
except Exception as e:
    _result = {"ok": False, "error": f"{type(e).__name__}: {e}", "code": 500}
_result
`);
  argsProxy.destroy();

  const r = result.toJs({ dict_converter: Object.fromEntries });
  result.destroy();

  if (!r.ok) {
    const err = new Error(r.error);
    err.code = r.code;
    throw err;
  }

  return {
    bytes: r.out_bytes,
    name: r.out_name,
    contentType: r.content_type,
    seed: r.seed,
    stats: r.stats,
    haired: r.haired,
    skipped: r.skipped,
    errored: r.errored,
  };
}

/** Render a single hair PNG on transparent background. Used by the chip
 *  previews on the landing page. */
export async function samplePng({ palette, morphology = null, seed = null }) {
  const py = await prefetch();
  const obj = { palette };
  if (morphology !== null && morphology !== undefined) obj.morphology = morphology;
  if (seed !== null && seed !== undefined) obj.seed = seed;
  const proxy = py.toPy(obj);
  py.globals.set("_sample_args", proxy);
  const result = await py.runPythonAsync(`
from app.engine import sample_hair_png
sample_hair_png(str(_sample_args["palette"]),
                morphology=(str(_sample_args["morphology"]) if "morphology" in _sample_args else None),
                seed=(int(_sample_args["seed"]) if "seed" in _sample_args else None))
`);
  proxy.destroy();
  const u8 = result.toJs();
  result.destroy();
  return u8;
}

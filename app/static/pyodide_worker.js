// Strand — Pyodide worker.
//
// The whole Python pipeline runs here, off the main thread. The main page
// stays fully responsive while a heavy pptx or PDF is being processed, so
// the browser never throws the "this tab is unresponsive" dialog and the
// spinner / status text keep updating right up to the moment the result
// lands.
//
// Protocol with the main thread (over postMessage):
//
//   in:  { id, op: "prefetch" | "run" | "sample", payload }
//   out: { type: "status", msg }                       — boot phase / progress
//        { id, ok: true, result }                      — RPC reply
//        { id, ok: false, error, code? }               — RPC reply with error
//
// `id` is echoed back unchanged so the shim can match replies to requests.
// The `status` channel is fire-and-forget — the shim broadcasts each message
// to whichever onStatus handler is currently registered.

const PYODIDE_VERSION = "0.29.4";
const CDN_INDEX_URL = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
// Same-origin copy vendored by scripts/fetch_pyodide.py (run at Docker build).
// When present we boot from here — faster, immutable-cacheable, no third-party
// dependency. When absent (e.g. plain local `uvicorn`), we fall back to the CDN.
const LOCAL_INDEX_URL = "/pyodide/";

let _pyodide = null;
let _bootPromise = null;
// Pyodide packages already loaded into the interpreter, so repeated runs don't
// re-resolve them. Pillow is loaded at boot; the rest come in on demand.
const _loaded = new Set();
let _pptxInstalled = false;

function status(msg) {
  self.postMessage({ type: "status", msg });
}

/** Prefer the vendored copy; fall back to the CDN if it isn't there. */
async function resolveIndexUrl() {
  try {
    const r = await fetch(LOCAL_INDEX_URL + "pyodide.js", { method: "HEAD" });
    if (r.ok) return LOCAL_INDEX_URL;
  } catch {
    /* not vendored — use the CDN */
  }
  return CDN_INDEX_URL;
}

async function boot() {
  const indexURL = await resolveIndexUrl();

  status("Loading engine (one-time ~10 MB)…");
  // importScripts is synchronous; calling it here (rather than at top level)
  // lets us pick the index URL first. Afterwards self.loadPyodide exists.
  self.importScripts(indexURL + "pyodide.js");
  const py = await self.loadPyodide({ indexURL });

  // Pillow is the only package core.py imports at module load, so it's all we
  // need to render hair on images. pymupdf (PDF) and python-pptx (pptx) are
  // imported lazily inside core.py and loaded on demand in ensureExtras().
  status("Loading image library…");
  await py.loadPackage(["Pillow"]);
  _loaded.add("pillow");

  status("Fetching renderer…");
  const [coreSrc, engineSrc] = await Promise.all([
    fetch("/core.py", { cache: "no-cache" }).then((r) => r.text()),
    fetch("/engine.py", { cache: "no-cache" }).then((r) => r.text()),
  ]);
  py.FS.mkdir("/app");
  py.FS.writeFile("/app/__init__.py", "");
  py.FS.writeFile("/app/core.py", coreSrc);
  py.FS.writeFile("/app/engine.py", engineSrc);

  await py.runPythonAsync(`
import sys
if "/" not in sys.path: sys.path.insert(0, "/")
from app import engine as _strand_engine  # warm import
`);

  status("Ready");
  _pyodide = py;
  return py;
}

async function ensureBooted() {
  if (_pyodide) return _pyodide;
  if (!_bootPromise) _bootPromise = boot();
  return _bootPromise;
}

const _extOf = (name) => {
  const n = (name || "").toLowerCase();
  const i = n.lastIndexOf(".");
  return i < 0 ? "" : n.slice(i);
};

/** Decide which heavy libraries this batch needs from the filenames. A .zip is
 *  opaque (we'd have to unpack it to know), so we conservatively load both. */
function neededLibs(files) {
  const exts = new Set((files || []).map((f) => _extOf(f.name)));
  const opaque = exts.has(".zip");
  return { pdf: opaque || exts.has(".pdf"), pptx: opaque || exts.has(".pptx") };
}

/** Load only the packages this run requires, skipping anything already loaded. */
async function ensureExtras(py, files) {
  const need = neededLibs(files);

  const toLoad = [];
  if (need.pdf && !_loaded.has("pymupdf")) toLoad.push("pymupdf");
  if (need.pptx && !_loaded.has("micropip")) toLoad.push("micropip");
  if (need.pptx && !_loaded.has("lxml")) toLoad.push("lxml");
  if (toLoad.length) {
    status("Loading document libraries…");
    await py.loadPackage(toLoad);
    for (const p of toLoad) _loaded.add(p);
  }

  // python-pptx isn't a Pyodide package; micropip pulls it (and its pure-Python
  // deps) from PyPI the first time a .pptx shows up.
  if (need.pptx && !_pptxInstalled) {
    status("Installing python-pptx…");
    await py.runPythonAsync(`
import micropip
await micropip.install("python-pptx")
`);
    _pptxInstalled = true;
  }
}

async function handleRun(payload) {
  const py = await ensureBooted();
  await ensureExtras(py, payload.files);
  status("Adding hair");

  // Pyodide's toPy() leaves nested JS `null` as `JsNull` (not Python None),
  // which breaks `is None` checks downstream. Simplest workaround: only
  // include keys whose values are present, and let Python `.get()` supply None.
  const argsObj = {
    items: payload.files,
    palette: payload.palette,
    intensity: payload.intensity,
  };
  if (payload.seed !== null && payload.seed !== undefined)
    argsObj.seed = payload.seed;
  if (payload.nameSuffix !== null && payload.nameSuffix !== undefined)
    argsObj.name_suffix = payload.nameSuffix;

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
  return r;
}

async function handleSample(payload) {
  const py = await ensureBooted();
  const obj = { palette: payload.palette };
  if (payload.morphology !== null && payload.morphology !== undefined)
    obj.morphology = payload.morphology;
  if (payload.seed !== null && payload.seed !== undefined)
    obj.seed = payload.seed;
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

self.onmessage = async (e) => {
  const { id, op, payload } = e.data || {};
  try {
    let result;
    if (op === "prefetch") {
      await ensureBooted();
      result = { ok: true };
    } else if (op === "run") {
      result = await handleRun(payload);
      // Transfer the output and preview buffers to avoid copying potentially
      // megabytes of result bytes back across the worker boundary.
      const transfer = [];
      if (result?.out_bytes?.buffer) transfer.push(result.out_bytes.buffer);
      if (result?.preview_before?.buffer)
        transfer.push(result.preview_before.buffer);
      if (result?.preview_after?.buffer)
        transfer.push(result.preview_after.buffer);
      self.postMessage({ id, ok: true, result }, transfer);
      return;
    } else if (op === "sample") {
      result = await handleSample(payload);
      self.postMessage(
        { id, ok: true, result },
        result?.buffer ? [result.buffer] : [],
      );
      return;
    } else {
      throw new Error(`Unknown op: ${op}`);
    }
    self.postMessage({ id, ok: true, result });
  } catch (err) {
    self.postMessage({ id, ok: false, error: String(err?.message || err) });
  }
};

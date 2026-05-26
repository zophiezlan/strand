// Strand — Pyodide engine (main-thread shim).
//
// The real Pyodide instance lives in a Web Worker (see pyodide_worker.js).
// This module is a thin RPC client that exposes the same API the page used
// before: prefetch / isReady / run / samplePng. Keeping Pyodide off the main
// thread means the UI thread stays free during heavy work — the spinner
// keeps animating, the status text keeps updating, scrolling still works,
// and the browser never thinks the tab is hung. That matters for big pptx
// files at high density, where Python can be busy for over a minute.
//
// Status messages emitted by the worker (boot phases, "Adding hair", etc.)
// are broadcast to every currently-registered `onStatus` callback.

const WORKER_URL = "/pyodide_worker.js";

let _worker = null;
let _nextId = 1;
const _pending = new Map();
const _statusHandlers = new Set();
let _isReady = false;

function ensureWorker() {
  if (_worker) return _worker;
  _worker = new Worker(WORKER_URL);
  _worker.onmessage = (e) => {
    const data = e.data || {};
    if (data.type === "status") {
      // The worker signals "Ready" once at the end of boot. We latch the
      // ready flag here so synchronous callers (isReady()) get an accurate
      // answer without having to await prefetch().
      if (data.msg === "Ready") _isReady = true;
      for (const h of _statusHandlers) {
        try { h(data.msg); } catch { /* don't let a bad handler kill others */ }
      }
      return;
    }
    const pend = _pending.get(data.id);
    if (!pend) return;
    _pending.delete(data.id);
    if (data.ok) pend.resolve(data.result);
    else {
      const err = new Error(data.error || "Engine error");
      if (data.code != null) err.code = data.code;
      pend.reject(err);
    }
  };
  _worker.onerror = (e) => {
    // If the worker itself dies (e.g. failed to load Pyodide), reject every
    // in-flight RPC so callers see a real error instead of hanging forever.
    const msg = e?.message || "Engine worker crashed.";
    for (const pend of _pending.values()) pend.reject(new Error(msg));
    _pending.clear();
  };
  return _worker;
}

function call(op, payload, transfer = []) {
  const w = ensureWorker();
  const id = _nextId++;
  return new Promise((resolve, reject) => {
    _pending.set(id, { resolve, reject });
    w.postMessage({ id, op, payload }, transfer);
  });
}

/** Kick off the worker boot now (idempotent). Resolves once Pyodide and the
 *  Python engine module are loaded and ready. */
export function prefetch(onStatus = () => {}) {
  _statusHandlers.add(onStatus);
  return call("prefetch", {}).finally(() => _statusHandlers.delete(onStatus));
}

/** True once Pyodide has finished booting in the worker. */
export function isReady() {
  return _isReady;
}

/**
 * Run the strand pipeline in the worker.
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
  _statusHandlers.add(onStatus);
  try {
    // Normalise to Uint8Array so the worker receives a consistent shape.
    const items = files.map((f) => ({
      name: f.name,
      bytes: f.bytes instanceof Uint8Array ? f.bytes : new Uint8Array(f.bytes),
    }));
    const result = await call("run", { files: items, palette, intensity, seed, nameSuffix });
    if (!result || !result.ok) {
      const err = new Error(result?.error || "Engine error");
      if (result?.code != null) err.code = result.code;
      throw err;
    }
    return {
      bytes: result.out_bytes,
      name: result.out_name,
      contentType: result.content_type,
      seed: result.seed,
      stats: result.stats,
      haired: result.haired,
      skipped: result.skipped,
      errored: result.errored,
      // PDF preview pair (Uint8Arrays of PNG bytes). Both null for other
      // input types — only PDFs currently get a rasterised before/after.
      previewBefore: result.preview_before || null,
      previewAfter: result.preview_after || null,
    };
  } finally {
    _statusHandlers.delete(onStatus);
  }
}

/** Render a single hair PNG on transparent background. Kept for parity with
 *  the older engine API; not currently called from the page (the drop-zone
 *  hair uses the server's /api/sample endpoint). */
export async function samplePng({ palette, morphology = null, seed = null }) {
  return await call("sample", { palette, morphology, seed });
}

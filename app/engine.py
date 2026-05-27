"""
Strand — Pyodide engine glue.

The same control flow as the `strand()` handler in `app.main`, kept here as a
plain Python file so the in-browser Pyodide worker can fetch it just like it
fetches `core.py`. The JS side never has to touch zip bundling, suffix
application, or the unsupported-type rules — that all lives in one place.

This file is loaded over HTTP by `app/static/pyodide_worker.js`, written into
the Pyodide virtual filesystem at `/app/engine.py`, and imported as
`app.engine`. It is *not* used by the FastAPI server-side path (which calls
into `app.core` directly).
"""

from __future__ import annotations

import io
import zipfile

from app import core


_OUTPUT_CONTENT_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".bmp":  "image/bmp",
    ".webp": "image/webp",
    ".tif":  "image/tiff",
    ".tiff": "image/tiff",
    ".ico":  "image/x-icon",
    ".pdf":  "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip":  "application/zip",
}


class StrandError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


def _bundle(items):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            safe = name.replace("\\", "/").lstrip("/")
            if not safe:
                continue
            z.writestr(safe, data)
    return buf.getvalue()


def _common_root(names):
    roots = set()
    for n in names:
        norm = n.replace("\\", "/")
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
    preview_before = preview_after = None

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
        # For PDFs, also rasterise the haired page and its untouched twin so
        # the UI can show a real before/after preview. python-pptx has no
        # equivalent renderer (it would need LibreOffice) so .pptx skips this.
        if single_ext == ".pdf":
            page_idx = stats.get("preview_page", 0) if isinstance(stats, dict) else 0
            try:
                preview_after = core.render_pdf_page_png(out, page_idx)
                preview_before = core.render_pdf_page_png(single_data, page_idx)
            except Exception:
                # Don't fail the whole run just because the preview couldn't
                # render — the user still gets their hair-injected PDF.
                preview_after = preview_before = None
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
        "preview_before": preview_before,
        "preview_after": preview_after,
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
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

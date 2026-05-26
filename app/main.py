"""
Strand — FastAPI app.

Reads the upload into memory, processes it, returns the result as a streaming
response. Nothing hits disk; nothing is logged about the file's contents.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .core import (
    SUPPORTED_INCLUDING_ZIP,
    SUPPORTED_SUFFIXES,
    ZIP_SUFFIXES,
    _apply_suffix,
    _suffix_of,
    strand_bytes,
    strand_zip_bytes,
    options_from_ui,
)

# --- Logging ---------------------------------------------------------------
# Log request envelopes (method, path, status) but never file contents or names.
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("strand")

# Quiet python-pptx + PIL chatter.
for noisy in ("PIL", "pptx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

STATIC_DIR = Path(__file__).parent / "static"

# --- Rate limiting ---------------------------------------------------------
limiter = Limiter(key_func=get_remote_address, default_limits=[])

app = FastAPI(
    title="Strand",
    description="Procedural-hair novelty filter for images, PDFs, and pptx files.",
    docs_url=None,
    redoc_url=None,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# --- Routes ----------------------------------------------------------------

@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


# Static mount happens at the *bottom* of this module so every explicit route
# (including `POST /strand`) is registered first. The mount sits at `/` (not
# `/static`) so that sibling-relative asset paths in index.html resolve both in
# production and when the HTML is opened directly from disk (e.g. by an editor
# preview pane). See the bottom of this file.


_OUTPUT_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".zip": "application/zip",
}

# Allowed characters in a user-supplied filename suffix. Conservative — we
# build the final download name from it, so no path bits or quotes.
_SUFFIX_BLOCKLIST = set('/\\:*?"<>|\r\n\0')
_MAX_SUFFIX_LEN = 32
_DEFAULT_SUFFIX = "-strand"


def _sanitize_name_suffix(raw) -> str:
    """User-supplied download suffix. Empty string is allowed (= no suffix).

    `raw` is either a string from the form, or a sentinel meaning "field absent" —
    we use Starlette's raw form dict (not FastAPI's Form coercion) to call this
    so we can tell those two cases apart.
    """
    if raw is _ABSENT:
        return _DEFAULT_SUFFIX
    raw = str(raw)
    cleaned = "".join(ch for ch in raw if ch not in _SUFFIX_BLOCKLIST).strip()
    if len(cleaned) > _MAX_SUFFIX_LEN:
        cleaned = cleaned[:_MAX_SUFFIX_LEN]
    return cleaned


_ABSENT = object()


def _parse_seed(raw: str | None) -> int | None:
    if not raw or not raw.strip():
        return None
    try:
        v = int(raw.strip())
    except ValueError:
        return None
    # Keep within 31 bits to match what we generate.
    return v & 0x7FFFFFFF


def _bundle_uploads_into_zip(items: list[tuple[str, bytes]]) -> bytes:
    """Pack multiple uploads into one in-memory zip, preserving sub-paths."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in items:
            # Normalize separators; strip any leading slashes for safety.
            safe = name.replace("\\", "/").lstrip("/")
            if not safe:
                continue
            z.writestr(safe, data)
    return buf.getvalue()


def _common_root(filenames: list[str]) -> str | None:
    """If every filename starts with the same single segment, return it."""
    roots = set()
    for f in filenames:
        norm = f.replace("\\", "/")
        if "/" not in norm:
            return None
        roots.add(norm.split("/", 1)[0])
    if len(roots) == 1:
        return roots.pop()
    return None


@app.post("/strand")
@limiter.limit("30/hour")
async def strand(
    request: Request,
    # Defaults aim at the most recognisable "stray hair" look — a single
    # light hair, the classic photocopier-glass / laminator-pocket artefact.
    palette: str = Form("white"),
    intensity: str = Form("subtle"),
    seed: str | None = Form(None),
):
    # Read name_suffix and the file list from the raw form. Empty strings come
    # through as None via FastAPI's Form() coercion, and getlist() lets us
    # accept multiple `file` fields for multi-file / folder upload.
    raw_form = await request.form()
    raw_suffix = raw_form["name_suffix"] if "name_suffix" in raw_form else _ABSENT

    uploads = [v for v in raw_form.getlist("file") if hasattr(v, "filename")]
    if not uploads:
        raise HTTPException(status_code=400, detail="No file uploaded.")

    # Read all bytes upfront so we can size-check the whole upload before any
    # processing. The 25 MB cap applies to the *total* payload, not each piece.
    items: list[tuple[str, bytes]] = []
    total = 0
    for f in uploads:
        data = await f.read()
        total += len(data)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)} MB total.",
            )
        if not data:
            continue
        items.append((f.filename or "upload", data))
    if not items:
        raise HTTPException(status_code=400, detail="Empty upload.")

    parsed_seed = _parse_seed(seed)
    opts = options_from_ui(palette=palette, intensity=intensity, seed=parsed_seed)
    clean_suffix = _sanitize_name_suffix(raw_suffix)

    # Decide whether the response is a single file or a zip:
    # - one upload that isn't itself a zip → single-file response
    # - one zip upload → unwrap, hairify each entry, return a zip
    # - multiple uploads → bundle into a zip, hairify each entry, return a zip
    single = len(items) == 1
    single_filename, single_data = items[0] if single else (None, None)
    single_suffix = _suffix_of(single_filename) if single else None
    single_is_zip = single and single_suffix in ZIP_SUFFIXES

    try:
        if single and not single_is_zip:
            if single_suffix not in SUPPORTED_SUFFIXES:
                raise HTTPException(
                    status_code=415,
                    detail=f"Unsupported file type: {single_suffix or '(none)'}. "
                           f"Supported: {', '.join(sorted(SUPPORTED_INCLUDING_ZIP))}.",
                )
            out, stats = strand_bytes(single_data, single_filename, opts)
            response_suffix = single_suffix
            download_name = _apply_suffix(single_filename, clean_suffix)
            extra_headers: dict[str, str] = {}
        else:
            # Either a real zip upload, or many uploads we wrap into one.
            if single_is_zip:
                payload = single_data
                base_name = single_filename
            else:
                payload = _bundle_uploads_into_zip(items)
                root = _common_root([n for n, _ in items])
                base_name = (root or "files") + ".zip"
            out, report = strand_zip_bytes(payload, opts, name_suffix=clean_suffix)
            stats = report.get("stats")
            response_suffix = ".zip"
            download_name = _apply_suffix(base_name, clean_suffix)
            extra_headers = {
                "X-Strand-Haired": str(report["haired_count"]),
                "X-Strand-Skipped": str(report["skipped_count"]),
                "X-Strand-Errored": str(report["error_count"]),
            }
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    except Exception as exc:
        # Surface a short error_id the user can quote when reporting.
        import uuid
        err_id = uuid.uuid4().hex[:8]
        log.exception("strand failed [%s]: %s", err_id, exc.__class__.__name__)
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed (error_id: {err_id}).",
            headers={"X-Strand-Error-Id": err_id},
        )

    log.info("strand %s n=%d %s seed=%s -> %d bytes",
             response_suffix, len(items), intensity, opts.seed, len(out))

    content_type = _OUTPUT_CONTENT_TYPES.get(response_suffix, "application/octet-stream")
    headers = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "Content-Length": str(len(out)),
        "Cache-Control": "no-store",
        "X-Strand-Seed": str(opts.seed),
        "Access-Control-Expose-Headers":
            "Content-Disposition, X-Strand-Seed, X-Strand-Haired, X-Strand-Skipped, X-Strand-Errored, X-Strand-Stats",
        **extra_headers,
    }
    if stats:
        import json as _json
        headers["X-Strand-Stats"] = _json.dumps(stats, separators=(",", ":"))
    return StreamingResponse(io.BytesIO(out), media_type=content_type, headers=headers)


# ---------------------------------------------------------------------------
# Public sample endpoint — returns one hair PNG on transparent background.
# Powers the palette chip previews on the landing page, and is cheap enough
# to leave unrated. Cached for an hour because the result is deterministic in
# (palette, seed).
# ---------------------------------------------------------------------------

import random as _random
from fastapi.responses import Response
from .core import MORPHOLOGIES, PALETTE_NAMES, generate_hair


@app.get("/api/sample")
def api_sample(
    palette: str = "dark",
    seed: int | None = None,
    morphology: str | None = None,
    loop: bool = False,
):
    """Return a single hair on transparent background.

    `morphology` selects one of: curve, loop, eyelash, fragment. If omitted,
    a random morphology is drawn using `generate_hair`'s default weights.
    `loop=true` is kept as a backwards-compatible shortcut for `morphology=loop`.
    """
    name = (palette or "").lower().strip()
    if name not in PALETTE_NAMES:
        raise HTTPException(status_code=400, detail=f"unknown palette: {palette!r}")
    rng = _random.Random(seed) if seed is not None else _random.Random()

    morph = (morphology or "").lower().strip() or ("loop" if loop else None)
    if morph:
        if morph not in MORPHOLOGIES:
            raise HTTPException(
                status_code=400,
                detail=f"unknown morphology: {morph!r}. Choose from {sorted(MORPHOLOGIES)}.",
            )
        hair = MORPHOLOGIES[morph](rng, palette=name)
    else:
        hair = generate_hair(rng, palette=name)

    buf = io.BytesIO()
    hair.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


# Static mount must be registered AFTER every explicit route above (Starlette
# tries routes in declaration order). See note near the top of this file.
app.mount("/", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )

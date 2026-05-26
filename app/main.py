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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from .core import (
    SUPPORTED_INCLUDING_ZIP,
    SUPPORTED_SUFFIXES,
    ZIP_SUFFIXES,
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


# Static assets (app.js, style.css). Mounted after the explicit "/" route so
# the index handler keeps priority.
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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


def _suffix_of(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


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


def _apply_suffix(name: str, suffix: str) -> str:
    if not suffix:
        return name
    if "." in name:
        stem, _, ext = name.rpartition(".")
        return f"{stem}{suffix}.{ext}"
    return f"{name}{suffix}"


def _parse_seed(raw: str | None) -> int | None:
    if not raw or not raw.strip():
        return None
    try:
        v = int(raw.strip())
    except ValueError:
        return None
    # Keep within 31 bits to match what we generate.
    return v & 0x7FFFFFFF


@app.post("/strand")
@limiter.limit("30/hour")
async def strand(
    request: Request,
    file: UploadFile = File(...),
    palette: str = Form("dark"),
    intensity: str = Form("normal"),
    seed: str | None = Form(None),
):
    # Read name_suffix from the raw form rather than via Form() so we can tell
    # "field absent" (default suffix) from "field present but empty" (no suffix).
    # Starlette/FastAPI collapses empty strings to None when using Form(None).
    raw_form = await request.form()
    if "name_suffix" in raw_form:
        raw_suffix = raw_form["name_suffix"]
    else:
        raw_suffix = _ABSENT

    filename = file.filename or "upload"
    suffix = _suffix_of(filename)
    if suffix not in SUPPORTED_INCLUDING_ZIP:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file type: {suffix or '(none)'}. "
                   f"Supported: {', '.join(sorted(SUPPORTED_INCLUDING_ZIP))}.",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Empty upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    parsed_seed = _parse_seed(seed)
    opts = options_from_ui(palette=palette, intensity=intensity, seed=parsed_seed)
    clean_suffix = _sanitize_name_suffix(raw_suffix)

    try:
        if suffix in ZIP_SUFFIXES:
            out, report = strand_zip_bytes(data, opts, name_suffix=clean_suffix)
            extra_headers = {
                "X-Strand-Haired": str(report["haired_count"]),
                "X-Strand-Skipped": str(report["skipped_count"]),
                "X-Strand-Errored": str(report["error_count"]),
            }
        else:
            out = strand_bytes(data, filename, opts)
            extra_headers = {}
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc))
    except Exception as exc:
        log.exception("strand failed: %s", exc.__class__.__name__)
        raise HTTPException(status_code=500, detail="Processing failed.")

    log.info("strand %s %s seed=%s -> %d bytes", suffix, intensity, opts.seed, len(out))

    content_type = _OUTPUT_CONTENT_TYPES.get(suffix, "application/octet-stream")
    download_name = _apply_suffix(filename, clean_suffix)
    headers = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "Content-Length": str(len(out)),
        "Cache-Control": "no-store",
        "X-Strand-Seed": str(opts.seed),
        "Access-Control-Expose-Headers":
            "Content-Disposition, X-Strand-Seed, X-Strand-Haired, X-Strand-Skipped, X-Strand-Errored",
        **extra_headers,
    }
    return StreamingResponse(io.BytesIO(out), media_type=content_type, headers=headers)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
        reload=bool(os.environ.get("RELOAD")),
    )

#!/usr/bin/env python3
"""Vendor the Pyodide runtime and the wheels Strand needs into app/static/pyodide/.

The in-browser engine (app/static/pyodide_worker.js) boots from this directory
when it exists, falling back to the jsdelivr CDN when it doesn't. Run this at
Docker build time so production serves the whole interpreter from its own origin
(faster, no third-party dependency, immutable-cacheable). Local `uvicorn` dev
works without running it — the worker just uses the CDN.

Only the wheels actually loaded at runtime are fetched: Pillow (always), plus
pymupdf / lxml / micropip (loaded lazily for PDFs and pptx). python-pptx is NOT
vendored — it is installed via micropip from PyPI on first pptx, the one
remaining runtime fetch.

Idempotent: a file already present with the expected sha256 is left alone.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

PYODIDE_VERSION = "0.29.4"
BASE_URL = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"

# Files the classic-worker boot needs. We importScripts(pyodide.js) and let
# loadPyodide() pull the rest relative to the index URL.
RUNTIME_FILES = (
    "pyodide.js",
    "pyodide.asm.js",
    "pyodide.asm.wasm",
    "python_stdlib.zip",
    "pyodide-lock.json",
)

# Canonical package names we loadPackage() at runtime. The lock's dependency
# graph is walked from these so any transitive wheels come along too.
WANTED_PACKAGES = ("pillow", "pymupdf", "lxml", "micropip")


def _fetch(url: str, timeout: int = 120) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_closure(lock: dict) -> dict[str, dict]:
    """Walk `depends` from WANTED_PACKAGES, returning {key: package_entry}."""
    packages = {k.lower(): v for k, v in lock["packages"].items()}
    closure: dict[str, dict] = {}
    stack = list(WANTED_PACKAGES)
    while stack:
        name = stack.pop().lower()
        if name in closure:
            continue
        entry = packages.get(name)
        if entry is None:
            raise SystemExit(f"package {name!r} not found in Pyodide lock")
        closure[name] = entry
        stack.extend(d.lower() for d in entry.get("depends", []))
    return closure


def _download_verified(url: str, dest: Path, expected_sha: str | None) -> tuple[bool, int]:
    """Download to dest unless already present with matching sha. Returns (downloaded, size)."""
    if dest.exists() and expected_sha is not None:
        if _sha256(dest.read_bytes()) == expected_sha:
            return False, dest.stat().st_size
    data = _fetch(url)
    if expected_sha is not None:
        got = _sha256(data)
        if got != expected_sha:
            raise SystemExit(f"sha256 mismatch for {url}\n  expected {expected_sha}\n  got      {got}")
    dest.write_bytes(data)
    return True, len(data)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    default_dest = Path(__file__).resolve().parent.parent / "app" / "static" / "pyodide"
    ap.add_argument("--dest", type=Path, default=default_dest,
                    help=f"output directory (default: {default_dest})")
    args = ap.parse_args()

    dest: Path = args.dest
    dest.mkdir(parents=True, exist_ok=True)

    print(f"Pyodide v{PYODIDE_VERSION} -> {dest}")

    # The lock both tells us how to verify the wheels and is itself a runtime
    # file. Fetch it once, write it, then drive everything else off it.
    lock_bytes = _fetch(BASE_URL + "pyodide-lock.json")
    lock = json.loads(lock_bytes)
    (dest / "pyodide-lock.json").write_bytes(lock_bytes)

    total = 0
    fetched = 0

    for name in RUNTIME_FILES:
        if name == "pyodide-lock.json":
            continue  # already written above
        downloaded, size = _download_verified(BASE_URL + name, dest / name, None)
        total += size
        fetched += downloaded
        print(f"  {'GET ' if downloaded else 'skip'} {name}  ({size/1e6:.1f} MB)")

    for key, entry in sorted(_resolve_closure(lock).items()):
        fname = entry["file_name"]
        downloaded, size = _download_verified(BASE_URL + fname, dest / fname, entry.get("sha256"))
        total += size
        fetched += downloaded
        print(f"  {'GET ' if downloaded else 'skip'} {fname}  ({size/1e6:.1f} MB)")

    print(f"done: {fetched} downloaded, {total/1e6:.1f} MB total on disk")
    return 0


if __name__ == "__main__":
    sys.exit(main())

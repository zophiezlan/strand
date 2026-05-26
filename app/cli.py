"""
Strand CLI — apply procedural hairs to images, PDFs, and pptx files on disk.

Thin wrapper over `app.core`: the same hair generation, palettes, density
tiers, and content-aware placement as the web service. The differences are
the filesystem-y things the web version legitimately can't (or shouldn't) do —
directory walking, backups + restore, mtime preservation, dry-run.

Usage:
    strand inject ./folder --intensity hirsute --palette mixed
    strand restore ./folder/.strand_backups/manifest.json
    strand preview ./samples.png --palette grey --count 16
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image

from .core import (
    INTENSITY_ORDER,
    PALETTE_NAMES,
    SUPPORTED_SUFFIXES,
    generate_hair,
    strand_bytes,
    options_from_ui,
)


# Directories the walker should never descend into.
SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".strand_backups", ".haired_backups", ".idea", ".vscode", "dist", "build",
}


def _should_skip(rel_parts: tuple[str, ...]) -> bool:
    for p in rel_parts:
        if p in SKIP_DIR_NAMES:
            return True
        if p.startswith(".") and len(p) > 1:
            return True
    return False


def _enumerate_candidates(root: Path) -> list[tuple[Path, Path]]:
    """Walk `root` and return [(absolute_path, relative_path), ...] for supported files."""
    out: list[tuple[Path, Path]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if _should_skip(rel.parts):
            continue
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        out.append((path, rel))
    return out


def _inject_file(path: Path, opts, preserve_mtime: bool) -> None:
    """Strand a single file in place. May raise; caller handles backup/restore."""
    stat = path.stat() if preserve_mtime else None
    data = path.read_bytes()
    out = strand_bytes(data, path.name, opts)
    path.write_bytes(out)
    if stat is not None:
        os.utime(path, (stat.st_atime, stat.st_mtime))


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_inject(args: argparse.Namespace) -> int:
    root: Path = args.directory.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    base_seed = args.seed if args.seed is not None else random.randrange(1, 2**31 - 1)
    candidates = _enumerate_candidates(root)

    print(f"found {len(candidates)} candidate file(s) under {root}")

    if args.dry_run:
        for _, rel in candidates:
            print(f"  [dry-run] {rel}")
        print(f"\n(seed would be: {base_seed})")
        return 0

    backup_dir = (args.backup_dir or (root / ".strand_backups")).resolve()
    manifest_path = (args.manifest or (backup_dir / "manifest.json")).resolve()
    do_backup = not args.no_backup

    if do_backup:
        backup_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root),
        "intensity": args.intensity,
        "palette": args.palette,
        "seed": base_seed,
        "preserve_mtime": args.preserve_mtime,
        "backups": do_backup,
        "entries": [],
    }

    haired = 0
    errors = 0

    for idx, (path, rel) in enumerate(candidates):
        # Per-file derived seed: same base seed reproduces the full run, and
        # files within the run still look different from one another.
        per_seed = (base_seed + idx * 1_000_003) & 0x7FFFFFFF
        opts = options_from_ui(palette=args.palette, intensity=args.intensity, seed=per_seed)

        backup_path = backup_dir / rel if do_backup else None
        if backup_path is not None:
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, backup_path)

        try:
            _inject_file(path, opts, preserve_mtime=args.preserve_mtime)
        except Exception as exc:
            errors += 1
            if backup_path is not None:
                shutil.copy2(backup_path, path)
                backup_path.unlink(missing_ok=True)
                # Best-effort cleanup of an emptied backup subdir.
                try:
                    backup_path.parent.rmdir()
                except OSError:
                    pass
            print(f"  ERROR   {rel}: {exc.__class__.__name__}: {exc}", file=sys.stderr)
            continue

        haired += 1
        manifest["entries"].append({
            "file": str(path),
            "rel": str(rel),
            "backup": str(backup_path) if backup_path else None,
            "suffix": path.suffix.lower(),
        })
        print(f"  haired  {rel}")

    if manifest["entries"] and do_backup:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print(f"manifest: {manifest_path}")

    print(f"\nhairified: {haired}  errored: {errors}  seed: {base_seed}")
    return 0 if errors == 0 else 1


def cmd_restore(args: argparse.Namespace) -> int:
    manifest_path: Path = args.manifest.resolve()
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    manifest = json.loads(manifest_path.read_text())
    entries = manifest.get("entries", [])

    restored = 0
    missing = 0
    for entry in entries:
        backup = Path(entry["backup"]) if entry.get("backup") else None
        target = Path(entry["file"])
        if backup is None or not backup.exists():
            print(f"  MISSING backup for {target}", file=sys.stderr)
            missing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        restored += 1
        print(f"  restored {target}")

    print(f"\nrestored: {restored}  missing: {missing}")
    return 0 if missing == 0 else 1


def cmd_preview(args: argparse.Namespace) -> int:
    """Render a grid of sample hairs for visual inspection of a palette."""
    rng = random.Random(args.seed)
    count = max(1, args.count)
    cols = min(count, 4)
    rows = math.ceil(count / cols)
    cell_w, cell_h = 420, 140
    grid = Image.new("RGBA", (cols * cell_w, rows * cell_h), (245, 245, 245, 255))

    for i in range(count):
        hair = generate_hair(rng, palette=args.palette)
        cell = Image.new("RGBA", (cell_w, cell_h), (255, 255, 255, 255))
        hw, hh = hair.size
        cell.alpha_composite(hair, ((cell_w - hw) // 2, (cell_h - hh) // 2))
        grid.paste(cell, ((i % cols) * cell_w, (i // cols) * cell_h))

    args.output.parent.mkdir(parents=True, exist_ok=True)
    grid.convert("RGB").save(args.output)
    print(f"wrote {count} sample(s) to {args.output}")
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="strand",
        description="Apply procedural hairs to images, PDFs, and pptx files on disk.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    inj = sub.add_parser("inject", help="Walk a directory and strand supported files.")
    inj.add_argument("directory", type=Path)
    inj.add_argument(
        "--intensity", choices=INTENSITY_ORDER, default="normal",
        help="Hair density tier. Past 'heavy' the labels are the joke. (default: normal)",
    )
    inj.add_argument(
        "--palette", choices=sorted(PALETTE_NAMES), default="dark",
        help="Hair colour palette. 'mixed' picks one per hair. (default: dark)",
    )
    inj.add_argument(
        "--seed", type=int, default=None,
        help="Reproducibility seed. Per-file seeds are derived from this base.",
    )
    inj.add_argument(
        "--preserve-mtime", action=argparse.BooleanOptionalAction, default=True,
        help="Restore each file's modified time after writing. (default: on)",
    )
    inj.add_argument(
        "--dry-run", action="store_true",
        help="List candidate files; change nothing.",
    )
    inj.add_argument(
        "--no-backup", action="store_true",
        help="Skip backups. Faster, but no `restore` afterwards.",
    )
    inj.add_argument(
        "--backup-dir", type=Path, default=None,
        help="Backup location (default: <directory>/.strand_backups).",
    )
    inj.add_argument(
        "--manifest", type=Path, default=None,
        help="Manifest path (default: <backup-dir>/manifest.json).",
    )

    res = sub.add_parser("restore", help="Restore files using a manifest produced by `inject`.")
    res.add_argument("manifest", type=Path)

    prv = sub.add_parser("preview", help="Render a grid of sample hairs to a PNG.")
    prv.add_argument("output", type=Path)
    prv.add_argument("--palette", choices=sorted(PALETTE_NAMES), default="dark")
    prv.add_argument("--count", type=int, default=12)
    prv.add_argument("--seed", type=int, default=None)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.cmd == "inject":
        return cmd_inject(args)
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "preview":
        return cmd_preview(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

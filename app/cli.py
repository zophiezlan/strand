# PYTHON_ARGCOMPLETE_OK
"""
Strand CLI — apply procedural hairs to images, PDFs, pptx, and zip files on disk.

Thin wrapper over `app.core`: same hair generation, palettes, density tiers,
and content-aware placement as the web service. The differences are the
filesystem-y things the web version legitimately can't (or shouldn't) do —
directory walking, backups + restore, mtime preservation, dry-run, glob
filters, output-dir mirroring.

Usage:
    strand inject ./folder --intensity hirsute --palette mixed
    strand inject photo.png --name-suffix=-strand     # sibling write
    strand inject pack.zip                            # in-place zip rewrite
    strand inject ./folder --output-dir ./out         # mirror tree
    strand inject ./folder --include '*.pdf' --exclude 'drafts/**'
    strand restore ./folder/.strand_backups/manifest.json
    strand undo                                       # restore the latest run
    strand preview ./samples.png --palette grey --count 16
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import math
import os
import random
import shutil
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path

from PIL import Image

try:
    import argcomplete  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — argcomplete is a hard dep, but fail open
    argcomplete = None

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .core import (
    INTENSITY_ORDER,
    MORPHOLOGIES,
    PALETTE_NAMES,
    SUPPORTED_INCLUDING_ZIP,
    SUPPORTED_SUFFIXES,
    ZIP_SUFFIXES,
    _apply_suffix,
    _suffix_of,
    generate_hair,
    options_from_ui,
    strand_bytes,
    strand_zip_bytes,
)


def _get_version() -> str:
    try:
        return _pkg_version("strand")
    except PackageNotFoundError:
        return "0.0.0+unknown"


# Directories the walker should never descend into.
SKIP_DIR_NAMES = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    ".strand_backups", ".haired_backups", ".idea", ".vscode", "dist", "build",
}


# ---------------------------------------------------------------------------
# Filename / suffix sanitation (kept in sync with main.py)
# ---------------------------------------------------------------------------

_SUFFIX_BLOCKLIST = set('/\\:*?"<>|\r\n\0')
_MAX_SUFFIX_LEN = 32


def _sanitize_name_suffix(raw: str) -> str:
    """Clean a user-supplied filename suffix. Empty string means 'overwrite in place'."""
    if raw is None:
        return ""
    cleaned = "".join(ch for ch in str(raw) if ch not in _SUFFIX_BLOCKLIST).strip()
    if len(cleaned) > _MAX_SUFFIX_LEN:
        cleaned = cleaned[:_MAX_SUFFIX_LEN]
    return cleaned


# ---------------------------------------------------------------------------
# Candidate enumeration
# ---------------------------------------------------------------------------

def _should_skip_dir(rel_parts: tuple[str, ...]) -> bool:
    for p in rel_parts:
        if p in SKIP_DIR_NAMES:
            return True
        if p.startswith(".") and len(p) > 1:
            return True
    return False


def _matches_any(rel_posix: str, patterns: list[str]) -> bool:
    """fnmatch against the relative path (POSIX form) and against the basename."""
    base = rel_posix.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatchcase(rel_posix, pat) or fnmatch.fnmatchcase(base, pat):
            return True
    return False


def _enumerate_candidates(
    root: Path,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[tuple[Path, Path]]:
    """Walk `root` and return [(absolute_path, relative_path), ...] of stranded files."""
    include = include or []
    exclude = exclude or []
    out: list[tuple[Path, Path]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        if _should_skip_dir(rel.parts):
            continue
        if path.suffix.lower() not in SUPPORTED_INCLUDING_ZIP:
            continue
        rel_posix = rel.as_posix()
        if include and not _matches_any(rel_posix, include):
            continue
        if exclude and _matches_any(rel_posix, exclude):
            continue
        out.append((path, rel))
    return out


# ---------------------------------------------------------------------------
# Per-file processing
# ---------------------------------------------------------------------------

def _process_one(
    src: Path,
    dest: Path,
    opts,
    preserve_mtime: bool,
) -> dict:
    """Strand a single file from `src` -> `dest`. Returns the stats dict.

    `src` and `dest` may be the same path (in-place overwrite). When `dest`
    is a separate path, the original is untouched.
    """
    stat = src.stat() if preserve_mtime else None
    data = src.read_bytes()
    suffix = src.suffix.lower()
    if suffix in ZIP_SUFFIXES:
        # `name_suffix=""` keeps inner entry names unchanged, since the user
        # already controls renaming via the outer destination path.
        out, report = strand_zip_bytes(data, opts, name_suffix="")
        stats = report["stats"]
        # Surface zip-level counts onto the per-file stats dict so callers
        # can show "3 entries hairified, 1 skipped" in the summary line.
        stats = dict(stats)
        stats["_zip"] = {
            "haired": report["haired_count"],
            "skipped": report["skipped_count"],
            "errored": report["error_count"],
        }
    else:
        out, stats = strand_bytes(data, src.name, opts)

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(out)
    if stat is not None and dest == src:
        os.utime(dest, (stat.st_atime, stat.st_mtime))
    return stats


def _aggregate_stats(into: dict, src: dict) -> None:
    into["hairs"] += src.get("hairs", 0)
    into["pages_touched"] += src.get("pages_touched", 0)
    into["clusters"] += src.get("clusters", 0)
    into["content_hits"] += src.get("content_hits", 0)
    into["hair_lengths_cm"].extend(src.get("hair_lengths_cm", []))
    for m, n in src.get("morphologies", {}).items():
        into["morphologies"][m] = into["morphologies"].get(m, 0) + n
    for p, n in src.get("palettes", {}).items():
        into["palettes"][p] = into["palettes"].get(p, 0) + n
    if "_zip" in src:
        zagg = into.setdefault("_zip", {"haired": 0, "skipped": 0, "errored": 0})
        for k, v in src["_zip"].items():
            zagg[k] = zagg.get(k, 0) + v


def _empty_aggregate() -> dict:
    return {
        "hairs": 0,
        "morphologies": {"curve": 0, "loop": 0, "eyelash": 0, "fragment": 0, "kink": 0},
        "palettes": {},
        "pages_touched": 0,
        "clusters": 0,
        "hair_lengths_cm": [],
        "content_hits": 0,
    }


def _summarize_stats_line(stats: dict) -> str:
    """One-line per-file stats summary for --verbose."""
    hairs = stats.get("hairs", 0)
    content = stats.get("content_hits", 0)
    ratio = (content / hairs * 100) if hairs else 0.0
    parts = [f"{hairs} hair{'' if hairs == 1 else 's'}"]
    if hairs:
        parts.append(f"{ratio:.0f}% content-aware")
    if stats.get("clusters"):
        parts.append(f"{stats['clusters']} cluster{'' if stats['clusters'] == 1 else 's'}")
    if "_zip" in stats:
        z = stats["_zip"]
        parts.append(f"zip: {z['haired']} haired / {z['skipped']} skipped / {z['errored']} errored")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# Output-path resolution
# ---------------------------------------------------------------------------

def _resolve_dest(
    src: Path,
    rel: Path,
    *,
    output_dir: Path | None,
    name_suffix: str,
) -> Path:
    """Decide where the hairified bytes for `src` (relative `rel`) should land.

    - output_dir set: mirror `rel` under output_dir, optionally renaming.
    - name_suffix non-empty: write `<stem><suffix><ext>` alongside the original.
    - otherwise: overwrite `src` in place.
    """
    if output_dir is not None:
        renamed_name = _apply_suffix(rel.name, name_suffix) if name_suffix else rel.name
        return output_dir / rel.parent / renamed_name
    if name_suffix:
        return src.with_name(_apply_suffix(src.name, name_suffix))
    return src


# ---------------------------------------------------------------------------
# Output / reporting
# ---------------------------------------------------------------------------

class Reporter:
    """Print sink with optional Rich styling and a progress bar.

    Modes:
    - 'json': all stdout output suppressed; final payload is the only thing
       that lands there. Diagnostics still go to stderr (no color).
    - 'human': uses Rich when stdout is a TTY (color + progress bar). On a
       pipe, Rich auto-disables color, and the progress bar is suppressed so
       grep/awk pipelines see plain `haired  <path>` lines.
    """

    def __init__(self, mode: str, verbose: bool, quiet: bool):
        self.mode = mode  # 'human' or 'json'
        self.verbose = verbose
        self.quiet = quiet
        # Rich's Console handles "is this a TTY?" internally — color and
        # progress bars auto-disable when output is piped or redirected.
        self.console = Console(quiet=(mode == "json" or quiet))
        self.err_console = Console(stderr=True)
        self.is_tty = self.console.is_terminal
        self._progress: Progress | None = None
        self._task_id = None

    # --- progress -----------------------------------------------------------

    def start_progress(self, total: int, description: str = "Stranding") -> None:
        if self.mode == "json" or self.quiet or not self.is_tty or total <= 0:
            return
        self._progress = Progress(
            SpinnerColumn(style="magenta"),
            TextColumn("[bold]{task.description}"),
            BarColumn(bar_width=None),
            MofNCompleteColumn(),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=self.console,
            transient=True,
        )
        self._progress.start()
        self._task_id = self._progress.add_task(description, total=total)

    def tick(self) -> None:
        if self._progress is not None and self._task_id is not None:
            self._progress.advance(self._task_id)

    def stop_progress(self) -> None:
        if self._progress is not None:
            self._progress.stop()
            self._progress = None
            self._task_id = None

    # --- messages -----------------------------------------------------------

    def info(self, msg: str) -> None:
        if self.mode == "json" or self.quiet:
            return
        self.console.print(msg)

    def file(self, rel: Path, stats: dict | None) -> None:
        if self.mode == "json" or self.quiet:
            return
        tail = f"  [dim]({_summarize_stats_line(stats)})[/dim]" if (self.verbose and stats) else ""
        # Use console.log-friendly markup; Rich strips on non-TTY.
        line = f"  [green]haired[/green]  {rel}{tail}"
        if self._progress is not None:
            self._progress.console.print(line)
        else:
            self.console.print(line)

    def dry(self, rel: Path) -> None:
        if self.mode == "json" or self.quiet:
            return
        # `\[` escapes Rich's markup so the literal `[dry-run]` survives.
        self.console.print(rf"  [yellow]\[dry-run][/yellow] {rel}")

    def error(self, rel: Path, exc: Exception) -> None:
        """Per-file error during inject. Always shown (even in --quiet) on stderr."""
        msg = f"  [red]ERROR[/red]   {rel}: {exc.__class__.__name__}: {exc}"
        # Errors are loud regardless of mode; only suppressed in JSON stdout.
        if self.mode == "json":
            self.err_console.print(f"  ERROR   {rel}: {exc.__class__.__name__}: {exc}",
                                   style=None, highlight=False)
        elif self._progress is not None:
            self._progress.console.print(msg)
        else:
            self.err_console.print(msg)

    def warn(self, msg: str) -> None:
        if self.mode == "json":
            print(msg, file=sys.stderr)
        else:
            self.err_console.print(f"[yellow]{msg}[/yellow]")

    # --- final summary table ----------------------------------------------

    def summary(
        self,
        *,
        haired: int,
        errored: int,
        seed: int,
        aggregate: dict,
        zip_stats: dict | None,
        manifest_path: Path | None,
    ) -> None:
        if self.mode == "json" or self.quiet:
            return
        table = Table(
            show_header=False,
            box=None,
            padding=(0, 1),
            title="[bold magenta]strand[/bold magenta] · summary",
            title_justify="left",
        )
        table.add_column(style="dim", justify="right")
        table.add_column()

        haired_style = "green" if haired and not errored else ("yellow" if haired else "red")
        table.add_row("hairified", f"[{haired_style}]{haired}[/{haired_style}]")
        if errored:
            table.add_row("errored", f"[red]{errored}[/red]")
        table.add_row("seed", f"[cyan]{seed}[/cyan]  [dim](re-run with --seed {seed} to reproduce)[/dim]")
        hairs = aggregate.get("hairs", 0)
        if hairs:
            content = aggregate.get("content_hits", 0)
            ratio = content / hairs * 100
            table.add_row(
                "hairs placed",
                f"{hairs}  [dim]({content}/{hairs} = {ratio:.0f}% content-aware)[/dim]",
            )
        if zip_stats:
            table.add_row(
                "zip entries",
                f"[green]{zip_stats['haired']}[/green] haired · "
                f"[yellow]{zip_stats['skipped']}[/yellow] skipped · "
                f"[red]{zip_stats['errored']}[/red] errored",
            )
        if manifest_path is not None:
            table.add_row("manifest", f"[dim]{manifest_path}[/dim]")

        self.console.print()
        self.console.print(table)


def _confirm_overwrite(reporter: Reporter, count: int) -> bool:
    """Interactive y/N prompt before a risky in-place run. False on non-TTY (caller decides)."""
    if not sys.stdin.isatty() or reporter.mode == "json":
        return True  # non-interactive: trust the user's flags
    reporter.console.print(
        f"[yellow]About to overwrite [bold]{count}[/bold] file(s) in place "
        f"with [bold]--no-backup[/bold]. This cannot be undone.[/yellow]"
    )
    try:
        answer = input("Continue? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

_CONFIRM_THRESHOLD = 10  # files; prompt before in-place + no-backup runs above this


def cmd_inject(args: argparse.Namespace) -> int:
    target: Path = args.target.resolve()
    if not target.exists():
        print(f"target not found: {target}", file=sys.stderr)
        return 2

    name_suffix = _sanitize_name_suffix(args.name_suffix)
    output_dir: Path | None = args.output_dir.resolve() if args.output_dir else None

    reporter = Reporter(
        mode="json" if args.json else "human",
        verbose=args.verbose,
        quiet=args.quiet,
    )

    base_seed = args.seed if args.seed is not None else random.randrange(1, 2**31 - 1)
    include = args.include or []
    exclude = args.exclude or []

    # Enumerate work. A single file becomes a 1-element list; a directory
    # walks; a zip is its own candidate (treated as one unit).
    if target.is_file():
        suffix = target.suffix.lower()
        if suffix not in SUPPORTED_INCLUDING_ZIP:
            reporter.warn(
                f"unsupported file type: {suffix or target.name!r}. "
                f"Supported: {', '.join(sorted(SUPPORTED_INCLUDING_ZIP))}."
            )
            return 2
        if include and not _matches_any(target.name, include):
            reporter.info(f"target {target.name} excluded by --include filters")
            return 0
        if exclude and _matches_any(target.name, exclude):
            reporter.info(f"target {target.name} excluded by --exclude filters")
            return 0
        # rel is just the filename — when output_dir is set we drop it directly
        # under the output dir rather than mirroring an arbitrary absolute path.
        root_for_walk = target.parent
        candidates = [(target, Path(target.name))]
        single_file_mode = True
    elif target.is_dir():
        root_for_walk = target
        candidates = _enumerate_candidates(target, include=include, exclude=exclude)
        single_file_mode = False
    else:
        reporter.warn(f"target is neither a file nor a directory: {target}")
        return 2

    reporter.info(f"found {len(candidates)} candidate file(s) under {target}")

    if args.dry_run:
        for _, rel in candidates:
            reporter.dry(rel)
        reporter.info(f"\n(seed would be: {base_seed})")
        if args.json:
            print(json.dumps({
                "dry_run": True,
                "seed": base_seed,
                "candidates": [r.as_posix() for _, r in candidates],
            }, indent=2))
        return 0

    # Backups only make sense for in-place overwrite. If the user is writing
    # outputs to a different path (output-dir or non-empty name-suffix), the
    # original is already preserved — skip backups automatically.
    writing_in_place = output_dir is None and not name_suffix
    do_backup = writing_in_place and not args.no_backup

    # Guardrail: running --no-backup on a directory of in-place edits is
    # unrecoverable. Prompt above a threshold unless --yes was passed.
    risky = (
        writing_in_place
        and args.no_backup
        and target.is_dir()
        and len(candidates) > _CONFIRM_THRESHOLD
        and not args.yes
    )
    if risky and not _confirm_overwrite(reporter, len(candidates)):
        reporter.info("[yellow]aborted.[/yellow]")
        return 1

    backup_dir = (args.backup_dir or (root_for_walk / ".strand_backups")).resolve()
    manifest_path = (args.manifest or (backup_dir / "manifest.json")).resolve()
    if do_backup:
        backup_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "root": str(root_for_walk),
        "target": str(target),
        "intensity": args.intensity,
        "palette": args.palette,
        "seed": base_seed,
        "preserve_mtime": args.preserve_mtime,
        "backups": do_backup,
        "name_suffix": name_suffix,
        "output_dir": str(output_dir) if output_dir else None,
        "entries": [],
    }

    aggregate = _empty_aggregate()
    per_file_records: list[dict] = []
    haired = 0
    errors = 0

    reporter.start_progress(total=len(candidates))
    try:
        for idx, (path, rel) in enumerate(candidates):
            # Per-file derived seed: same base seed reproduces the full run, and
            # files within the run still look different from one another.
            per_seed = (base_seed + idx * 1_000_003) & 0x7FFFFFFF
            opts = options_from_ui(palette=args.palette, intensity=args.intensity, seed=per_seed)

            dest = _resolve_dest(
                path, rel,
                output_dir=output_dir,
                name_suffix=name_suffix,
            )

            backup_path = backup_dir / rel if do_backup else None
            if backup_path is not None:
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup_path)

            try:
                stats = _process_one(path, dest, opts, preserve_mtime=args.preserve_mtime)
            except Exception as exc:
                errors += 1
                if backup_path is not None:
                    shutil.copy2(backup_path, path)
                    backup_path.unlink(missing_ok=True)
                    try:
                        backup_path.parent.rmdir()
                    except OSError:
                        pass
                reporter.error(rel, exc)
                per_file_records.append({
                    "file": str(path),
                    "rel": rel.as_posix(),
                    "status": "errored",
                    "error": f"{exc.__class__.__name__}: {exc}",
                })
                reporter.tick()
                continue

            haired += 1
            _aggregate_stats(aggregate, stats)
            record = {
                "file": str(path),
                "rel": rel.as_posix(),
                "dest": str(dest),
                "status": "haired",
                "seed": opts.seed,
                "hairs": stats.get("hairs", 0),
                "content_hits": stats.get("content_hits", 0),
                "clusters": stats.get("clusters", 0),
                "morphologies": stats.get("morphologies", {}),
                "palettes": stats.get("palettes", {}),
            }
            if "_zip" in stats:
                record["zip"] = stats["_zip"]
            per_file_records.append(record)
            manifest["entries"].append({
                "file": str(path),
                "rel": str(rel),
                "dest": str(dest),
                "backup": str(backup_path) if backup_path else None,
                "suffix": path.suffix.lower(),
            })
            reporter.file(rel, stats)
            reporter.tick()
    finally:
        reporter.stop_progress()

    wrote_manifest = bool(manifest["entries"] and do_backup)
    if wrote_manifest:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2))

    if args.json:
        payload = {
            "haired": haired,
            "errored": errors,
            "seed": base_seed,
            "target": str(target),
            "output_dir": str(output_dir) if output_dir else None,
            "name_suffix": name_suffix,
            "manifest": str(manifest_path) if wrote_manifest else None,
            "aggregate": aggregate,
            "entries": per_file_records,
        }
        print(json.dumps(payload, indent=2))
    else:
        reporter.summary(
            haired=haired,
            errored=errors,
            seed=base_seed,
            aggregate=aggregate,
            zip_stats=aggregate.get("_zip") if not single_file_mode else None,
            manifest_path=manifest_path if wrote_manifest else None,
        )

    return 0 if errors == 0 else 1


def _find_latest_manifest(root: Path) -> Path | None:
    """Find the newest manifest.json under any `.strand_backups/` below `root`."""
    candidates = list(root.rglob(".strand_backups/manifest.json"))
    if not candidates:
        return None
    # Newest first by mtime.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def cmd_undo(args: argparse.Namespace) -> int:
    root: Path = args.path.resolve()
    if not root.exists():
        print(f"path not found: {root}", file=sys.stderr)
        return 2
    if root.is_file() and root.name == "manifest.json":
        manifest_path = root
    else:
        if not root.is_dir():
            print(f"not a directory: {root}", file=sys.stderr)
            return 2
        found = _find_latest_manifest(root)
        if found is None:
            print(f"no .strand_backups/manifest.json found under {root}", file=sys.stderr)
            return 2
        manifest_path = found

    console = Console()
    console.print(f"[dim]using manifest:[/dim] {manifest_path}")
    return _restore_from_manifest(manifest_path, console)


def _restore_from_manifest(manifest_path: Path, console: Console | None = None) -> int:
    console = console or Console()
    manifest = json.loads(manifest_path.read_text())
    entries = manifest.get("entries", [])

    restored = 0
    missing = 0
    for entry in entries:
        backup = Path(entry["backup"]) if entry.get("backup") else None
        target = Path(entry["file"])
        if backup is None or not backup.exists():
            console.print(f"  [red]MISSING[/red] backup for {target}")
            missing += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        restored += 1
        console.print(f"  [green]restored[/green] {target}")

    console.print(f"\n[bold]restored:[/bold] {restored}  [bold]missing:[/bold] {missing}")
    return 0 if missing == 0 else 1


def cmd_restore(args: argparse.Namespace) -> int:
    manifest_path: Path = args.manifest.resolve()
    if not manifest_path.is_file():
        print(f"manifest not found: {manifest_path}", file=sys.stderr)
        return 2
    return _restore_from_manifest(manifest_path)


def cmd_sample(args: argparse.Namespace) -> int:
    """Emit a single hair PNG to a path or stdout. Parity with /api/sample."""
    rng = random.Random(args.seed)
    morph = (args.morphology or "").lower().strip()
    if morph:
        if morph not in MORPHOLOGIES:
            print(f"unknown morphology: {morph}. Choose from {sorted(MORPHOLOGIES)}.",
                  file=sys.stderr)
            return 2
        hair = MORPHOLOGIES[morph](rng, palette=args.palette)
    else:
        hair = generate_hair(rng, palette=args.palette)

    if str(args.output) == "-":
        buf = io.BytesIO()
        hair.save(buf, format="PNG")
        sys.stdout.buffer.write(buf.getvalue())
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        hair.save(args.output, format="PNG")
        print(f"wrote 1 hair to {args.output}")
    return 0


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


def cmd_list(args: argparse.Namespace) -> int:
    """Print the available palettes, morphologies, intensities, and supported file types."""
    print("palettes:    ", ", ".join(sorted(PALETTE_NAMES)))
    print("intensities: ", ", ".join(INTENSITY_ORDER))
    print("morphologies:", ", ".join(sorted(MORPHOLOGIES)))
    print("file types:  ", ", ".join(sorted(SUPPORTED_INCLUDING_ZIP)))
    return 0


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="strand",
        description="Apply procedural hairs to images, PDFs, pptx, and zip files on disk.",
    )
    p.add_argument(
        "--version", action="version",
        version=f"%(prog)s {_get_version()}",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    inj = sub.add_parser(
        "inject",
        help="Strand a file, a directory of files, or a zip.",
        description=(
            "Strand a file, a directory of files, or a zip. With no --name-suffix "
            "and no --output-dir, files are overwritten in place (with backups). "
            "Provide either flag to write to a different path and leave originals alone."
        ),
    )
    inj.add_argument(
        "target", type=Path,
        help="File, directory, or zip to process.",
    )
    inj.add_argument(
        "--intensity", choices=INTENSITY_ORDER, default="subtle",
        help="Hair density tier. Past 'heavy' the labels are the joke. "
             "(default: subtle — one hair, the realistic photocopier look)",
    )
    inj.add_argument(
        "--palette", choices=sorted(PALETTE_NAMES), default="white",
        help="Hair colour palette. 'mixed' picks one per hair. "
             "(default: white — best mimics a photocopied/laminated stray hair)",
    )
    inj.add_argument(
        "--seed", type=int, default=None,
        help="Reproducibility seed. Per-file seeds are derived from this base.",
    )
    inj.add_argument(
        "--preserve-mtime", action=argparse.BooleanOptionalAction, default=True,
        help="Restore each file's modified time after writing. "
             "Only applies to in-place overwrites. (default: on)",
    )
    inj.add_argument(
        "--dry-run", action="store_true",
        help="List candidate files; change nothing.",
    )
    inj.add_argument(
        "--no-backup", action="store_true",
        help="Skip backups. Faster, but no `restore` afterwards. "
             "Automatic when --name-suffix or --output-dir is set.",
    )
    inj.add_argument(
        "--backup-dir", type=Path, default=None,
        help="Backup location (default: <target>/.strand_backups).",
    )
    inj.add_argument(
        "--manifest", type=Path, default=None,
        help="Manifest path (default: <backup-dir>/manifest.json).",
    )
    inj.add_argument(
        "--name-suffix", default="",
        help="Suffix inserted between stem and extension (e.g. '-strand' → 'photo-strand.png'). "
             "When set, the original is left untouched and a renamed copy is written alongside. "
             "(default: empty — overwrite in place)",
    )
    inj.add_argument(
        "--output-dir", type=Path, default=None,
        help="Write outputs into this directory, mirroring the source tree. "
             "Originals are left untouched.",
    )
    inj.add_argument(
        "--include", action="append", default=None, metavar="PATTERN",
        help="Glob filter; only files matching at least one --include are processed. "
             "Matched against both the relative path and the basename. Repeatable.",
    )
    inj.add_argument(
        "--exclude", action="append", default=None, metavar="PATTERN",
        help="Glob filter; files matching any --exclude are skipped. Repeatable.",
    )
    inj.add_argument(
        "--json", action="store_true",
        help="Emit a structured JSON report to stdout instead of human-readable prose.",
    )
    inj.add_argument(
        "--quiet", "-q", action="store_true",
        help="Suppress per-file lines; print only the final summary.",
    )
    inj.add_argument(
        "--verbose", "-v", action="store_true",
        help="Include per-file stats (hair count, content-aware ratio) in the output.",
    )
    inj.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt for risky in-place runs without backups.",
    )

    res = sub.add_parser("restore", help="Restore files using a manifest produced by `inject`.")
    res.add_argument("manifest", type=Path)

    und = sub.add_parser(
        "undo",
        help="Restore the most recent inject run. Finds the newest manifest.json under "
             "`.strand_backups/` in the given path (default: cwd).",
    )
    und.add_argument(
        "path", type=Path, nargs="?", default=Path("."),
        help="Directory to search for a manifest. (default: current directory)",
    )

    prv = sub.add_parser("preview", help="Render a grid of sample hairs to a PNG.")
    prv.add_argument("output", type=Path)
    prv.add_argument("--palette", choices=sorted(PALETTE_NAMES), default="dark")
    prv.add_argument("--count", type=int, default=12)
    prv.add_argument("--seed", type=int, default=None)

    smp = sub.add_parser(
        "sample",
        help="Render a single hair PNG. Use '-' as output to write to stdout.",
    )
    smp.add_argument("output", type=Path)
    smp.add_argument("--palette", choices=sorted(PALETTE_NAMES), default="dark")
    smp.add_argument(
        "--morphology", choices=sorted(MORPHOLOGIES), default=None,
        help="Force a specific morphology. Default: random per the standard weights.",
    )
    smp.add_argument("--seed", type=int, default=None)

    sub.add_parser(
        "list",
        help="Print available palettes, intensities, morphologies, and supported file types.",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Tab completion is registered as a no-op if the shell hasn't been wired up;
    # see the README's "Shell completion" section for the per-shell snippet.
    if argcomplete is not None:
        argcomplete.autocomplete(parser)
    args = parser.parse_args(argv)
    if args.cmd == "inject":
        return cmd_inject(args)
    if args.cmd == "restore":
        return cmd_restore(args)
    if args.cmd == "undo":
        return cmd_undo(args)
    if args.cmd == "preview":
        return cmd_preview(args)
    if args.cmd == "sample":
        return cmd_sample(args)
    if args.cmd == "list":
        return cmd_list(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

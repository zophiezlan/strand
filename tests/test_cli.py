"""
Tests for the `strand` CLI. Invokes `app.cli.main` directly with argv to
avoid subprocess overhead.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.cli import main


# --- Fixtures --------------------------------------------------------------

def _make_png(path: Path, size=(400, 300)) -> None:
    img = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 200, 80), fill="black")
    d.rectangle((40, 120, 360, 250), outline="black", width=3)
    img.save(path, format="PNG")


def _make_pdf(path: Path, pages: int = 2) -> None:
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter
    c = canvas.Canvas(str(path), pagesize=letter)
    for i in range(pages):
        c.setFont("Helvetica", 12)
        c.drawString(72, 720, f"Page {i + 1}")
        c.drawString(72, 700, "Lorem ipsum dolor sit amet.")
        c.showPage()
    c.save()


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small tree with one PNG, one PDF, one dotdir, and one unsupported file."""
    _make_png(tmp_path / "photo.png")
    (tmp_path / "sub").mkdir()
    _make_pdf(tmp_path / "sub" / "notes.pdf", pages=2)
    (tmp_path / "README.txt").write_text("untouched")
    (tmp_path / ".git").mkdir()
    _make_png(tmp_path / ".git" / "should-be-skipped.png")
    return tmp_path


# --- preview ---------------------------------------------------------------

def test_cli_preview_writes_a_png(tmp_path: Path):
    out = tmp_path / "samples.png"
    rc = main(["preview", str(out), "--palette", "mixed", "--count", "6", "--seed", "1"])
    assert rc == 0
    assert out.is_file()
    with Image.open(out) as im:
        im.load()
        assert im.format == "PNG"


# --- sample subcommand ----------------------------------------------------

def test_cli_sample_writes_single_hair_png(tmp_path: Path):
    out = tmp_path / "one.png"
    rc = main(["sample", str(out), "--palette", "blonde", "--seed", "3"])
    assert rc == 0
    with Image.open(out) as im:
        im.load()
        assert im.format == "PNG"
        # Hairs come with an alpha channel — they're meant to composite.
        assert "A" in im.getbands()


def test_cli_sample_morphology_choice(tmp_path: Path):
    out = tmp_path / "eye.png"
    rc = main(["sample", str(out), "--morphology", "eyelash", "--seed", "1"])
    assert rc == 0
    assert out.is_file()


def test_cli_sample_rejects_unknown_morphology(tmp_path: Path):
    # argparse rejects bad choices with code 2 before our handler runs.
    with pytest.raises(SystemExit) as ei:
        main(["sample", str(tmp_path / "x.png"), "--morphology", "spiral"])
    assert ei.value.code == 2


def test_cli_sample_stdout(capsysbinary):
    """Output '-' writes PNG bytes straight to stdout for pipelines."""
    rc = main(["sample", "-", "--seed", "1"])
    assert rc == 0
    captured = capsysbinary.readouterr()
    assert captured.out.startswith(b"\x89PNG\r\n\x1a\n")


# --- inject ----------------------------------------------------------------

def test_cli_inject_dry_run_changes_nothing(tree: Path, capsys):
    before = (tree / "photo.png").read_bytes()
    rc = main(["inject", str(tree), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    # File untouched.
    assert (tree / "photo.png").read_bytes() == before
    # No backup dir created.
    assert not (tree / ".strand_backups").exists()


def test_cli_inject_modifies_and_creates_manifest(tree: Path):
    before_png = (tree / "photo.png").read_bytes()
    before_pdf = (tree / "sub" / "notes.pdf").read_bytes()

    rc = main(["inject", str(tree), "--intensity", "normal", "--seed", "42"])
    assert rc == 0

    assert (tree / "photo.png").read_bytes() != before_png
    assert (tree / "sub" / "notes.pdf").read_bytes() != before_pdf

    manifest_path = tree / ".strand_backups" / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["intensity"] == "normal"
    # Palette defaults to "white" (the photocopier-stray-hair sweet spot)
    # when no --palette is passed.
    assert manifest["palette"] == "white"
    assert manifest["seed"] == 42

    rels = sorted(e["rel"].replace("\\", "/") for e in manifest["entries"])
    assert "photo.png" in rels
    assert "sub/notes.pdf" in rels
    # Unsupported file and dotdir file not in the manifest.
    assert "README.txt" not in rels
    assert all(".git" not in r for r in rels)


def test_cli_inject_skips_unsupported_and_dotdirs(tree: Path):
    txt_before = (tree / "README.txt").read_text()
    skipped_before = (tree / ".git" / "should-be-skipped.png").read_bytes()

    main(["inject", str(tree), "--seed", "7"])

    assert (tree / "README.txt").read_text() == txt_before
    assert (tree / ".git" / "should-be-skipped.png").read_bytes() == skipped_before


def test_cli_inject_preserves_mtime(tree: Path):
    target = tree / "photo.png"
    # Set a known historical mtime — 2023-01-01.
    fixed = time.mktime((2023, 1, 1, 12, 0, 0, 0, 0, 0))
    os.utime(target, (fixed, fixed))

    main(["inject", str(tree), "--seed", "1", "--preserve-mtime"])

    assert target.stat().st_mtime == pytest.approx(fixed, abs=1.0)


def test_cli_inject_no_preserve_mtime_bumps_mtime(tree: Path):
    target = tree / "photo.png"
    fixed = time.mktime((2023, 1, 1, 12, 0, 0, 0, 0, 0))
    os.utime(target, (fixed, fixed))

    main(["inject", str(tree), "--seed", "1", "--no-preserve-mtime"])

    assert target.stat().st_mtime > fixed + 60


def test_cli_inject_no_backup_skips_manifest(tree: Path):
    rc = main(["inject", str(tree), "--seed", "1", "--no-backup"])
    assert rc == 0
    assert not (tree / ".strand_backups").exists()


def test_cli_inject_is_reproducible_with_seed(tmp_path: Path):
    """Same seed on identical inputs reproduces identical outputs."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _make_png(a / "photo.png")
    _make_png(b / "photo.png")

    main(["inject", str(a), "--seed", "12345", "--no-backup"])
    main(["inject", str(b), "--seed", "12345", "--no-backup"])

    assert (a / "photo.png").read_bytes() == (b / "photo.png").read_bytes()


def test_cli_inject_wild_intensity_runs(tree: Path):
    rc = main(["inject", str(tree), "--intensity", "cousin-itt", "--seed", "9", "--no-backup"])
    assert rc == 0


# --- restore ---------------------------------------------------------------

def test_cli_restore_roundtrips(tree: Path):
    original = (tree / "photo.png").read_bytes()

    rc = main(["inject", str(tree), "--seed", "1"])
    assert rc == 0
    assert (tree / "photo.png").read_bytes() != original

    manifest = tree / ".strand_backups" / "manifest.json"
    rc = main(["restore", str(manifest)])
    assert rc == 0
    assert (tree / "photo.png").read_bytes() == original


# --- argparse surface ------------------------------------------------------

def test_cli_rejects_unknown_palette(tree: Path):
    with pytest.raises(SystemExit) as ei:
        main(["inject", str(tree), "--palette", "neon-pink"])
    assert ei.value.code == 2


def test_cli_rejects_unknown_intensity(tree: Path):
    with pytest.raises(SystemExit) as ei:
        main(["inject", str(tree), "--intensity", "extreme"])
    assert ei.value.code == 2


def test_cli_rejects_missing_subcommand():
    with pytest.raises(SystemExit) as ei:
        main([])
    assert ei.value.code == 2


# --- single-file inject ----------------------------------------------------

def test_cli_inject_single_file_in_place(tmp_path: Path):
    png = tmp_path / "solo.png"
    _make_png(png)
    before = png.read_bytes()

    rc = main(["inject", str(png), "--seed", "1"])
    assert rc == 0
    assert png.read_bytes() != before
    # Backup dir lands next to the file's parent (the resolved target's parent
    # in single-file mode is the directory containing the file).
    backup_dir = tmp_path / ".strand_backups"
    assert backup_dir.is_dir()
    assert (backup_dir / "manifest.json").is_file()


def test_cli_inject_rejects_unsupported_single_file(tmp_path: Path):
    txt = tmp_path / "notes.txt"
    txt.write_text("nope")
    rc = main(["inject", str(txt)])
    assert rc == 2
    assert txt.read_text() == "nope"


def test_cli_inject_rejects_missing_target(tmp_path: Path):
    rc = main(["inject", str(tmp_path / "missing.png")])
    assert rc == 2


# --- name-suffix -----------------------------------------------------------

def test_cli_inject_name_suffix_single_file(tmp_path: Path):
    png = tmp_path / "photo.png"
    _make_png(png)
    before = png.read_bytes()

    rc = main(["inject", str(png), f"--name-suffix=-strand", "--seed", "1"])
    assert rc == 0
    # Original untouched.
    assert png.read_bytes() == before
    # Sibling written.
    assert (tmp_path / "photo-strand.png").is_file()
    # No backup dir — originals weren't modified.
    assert not (tmp_path / ".strand_backups").exists()


def test_cli_inject_name_suffix_directory(tree: Path):
    before_png = (tree / "photo.png").read_bytes()
    before_pdf = (tree / "sub" / "notes.pdf").read_bytes()

    rc = main(["inject", str(tree), f"--name-suffix=-strand", "--seed", "1"])
    assert rc == 0
    # Originals untouched.
    assert (tree / "photo.png").read_bytes() == before_png
    assert (tree / "sub" / "notes.pdf").read_bytes() == before_pdf
    # Renamed copies written alongside.
    assert (tree / "photo-strand.png").is_file()
    assert (tree / "sub" / "notes-strand.pdf").is_file()
    # No backup dir.
    assert not (tree / ".strand_backups").exists()


def test_cli_inject_name_suffix_sanitized(tmp_path: Path):
    """Path-bearing characters in the suffix get stripped."""
    png = tmp_path / "photo.png"
    _make_png(png)
    # Slashes, backslashes, and quotes are stripped; "x" survives.
    rc = main(["inject", str(png), "--name-suffix", "../x", "--seed", "1"])
    assert rc == 0
    # The original is preserved (non-empty suffix → sibling write).
    assert (tmp_path / "photo.png").exists()
    # The dangerous chars dropped, leaving "..x".
    assert (tmp_path / "photo..x.png").is_file()


# --- output-dir ------------------------------------------------------------

def test_cli_inject_output_dir_mirrors_tree(tree: Path, tmp_path: Path):
    out = tmp_path / "haired"
    before_png = (tree / "photo.png").read_bytes()

    rc = main(["inject", str(tree), "--output-dir", str(out), "--seed", "1"])
    assert rc == 0
    # Originals untouched.
    assert (tree / "photo.png").read_bytes() == before_png
    # Outputs mirrored under out/.
    assert (out / "photo.png").is_file()
    assert (out / "sub" / "notes.pdf").is_file()
    # Outputs differ from inputs.
    assert (out / "photo.png").read_bytes() != before_png
    # No backup dir.
    assert not (tree / ".strand_backups").exists()


def test_cli_inject_output_dir_combined_with_name_suffix(tree: Path, tmp_path: Path):
    out = tmp_path / "haired"
    rc = main([
        "inject", str(tree),
        "--output-dir", str(out),
        f"--name-suffix=-v2",
        "--seed", "1",
    ])
    assert rc == 0
    assert (out / "photo-v2.png").is_file()
    assert (out / "sub" / "notes-v2.pdf").is_file()


def test_cli_inject_output_dir_single_file(tmp_path: Path):
    png = tmp_path / "photo.png"
    _make_png(png)
    out = tmp_path / "out"
    rc = main(["inject", str(png), "--output-dir", str(out), "--seed", "1"])
    assert rc == 0
    assert (out / "photo.png").is_file()
    # Original untouched.
    assert png.is_file()


# --- zip handling ----------------------------------------------------------

def _make_zip(path: Path) -> None:
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        # In-memory PNG.
        buf = io.BytesIO()
        img = Image.new("RGB", (200, 150), "white")
        ImageDraw.Draw(img).rectangle((10, 10, 100, 60), fill="black")
        img.save(buf, format="PNG")
        z.writestr("inside.png", buf.getvalue())
        z.writestr("readme.txt", "untouched-passthrough")


def test_cli_inject_single_zip_in_place(tmp_path: Path):
    import zipfile
    z = tmp_path / "pack.zip"
    _make_zip(z)
    before = z.read_bytes()

    rc = main(["inject", str(z), "--seed", "1"])
    assert rc == 0
    after = z.read_bytes()
    assert after != before

    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        # Inner png is hairified (still named the same — strand_zip's
        # name_suffix is empty in CLI mode).
        assert "inside.png" in names
        # Pass-through txt preserved.
        assert "readme.txt" in names
        assert zf.read("readme.txt") == b"untouched-passthrough"
        # Report added.
        assert "_strand-report.txt" in names
        report = zf.read("_strand-report.txt").decode()
        assert "hairified: 1" in report
        assert "skipped:" in report


def test_cli_inject_zip_with_name_suffix(tmp_path: Path):
    z = tmp_path / "pack.zip"
    _make_zip(z)
    before = z.read_bytes()

    rc = main(["inject", str(z), f"--name-suffix=-strand", "--seed", "1"])
    assert rc == 0
    # Original zip untouched.
    assert z.read_bytes() == before
    # Renamed copy written.
    assert (tmp_path / "pack-strand.zip").is_file()


def test_cli_inject_zip_inside_directory(tmp_path: Path):
    _make_zip(tmp_path / "pack.zip")
    _make_png(tmp_path / "loose.png")
    rc = main(["inject", str(tmp_path), "--seed", "1", "--no-backup"])
    assert rc == 0
    # Both files were touched.
    import zipfile
    with zipfile.ZipFile(tmp_path / "pack.zip") as zf:
        assert "_strand-report.txt" in zf.namelist()


# --- include / exclude -----------------------------------------------------

def test_cli_inject_include_filter(tree: Path):
    before_pdf = (tree / "sub" / "notes.pdf").read_bytes()
    before_png = (tree / "photo.png").read_bytes()

    rc = main(["inject", str(tree), "--include", "*.pdf", "--seed", "1", "--no-backup"])
    assert rc == 0
    # PDF processed, PNG untouched.
    assert (tree / "sub" / "notes.pdf").read_bytes() != before_pdf
    assert (tree / "photo.png").read_bytes() == before_png


def test_cli_inject_exclude_filter(tree: Path):
    before_pdf = (tree / "sub" / "notes.pdf").read_bytes()
    before_png = (tree / "photo.png").read_bytes()

    rc = main(["inject", str(tree), "--exclude", "sub/*", "--seed", "1", "--no-backup"])
    assert rc == 0
    # PNG processed, PDF in sub/ skipped.
    assert (tree / "sub" / "notes.pdf").read_bytes() == before_pdf
    assert (tree / "photo.png").read_bytes() != before_png


def test_cli_inject_include_filter_on_single_file_excludes_it(tmp_path: Path):
    png = tmp_path / "photo.png"
    _make_png(png)
    before = png.read_bytes()
    rc = main(["inject", str(png), "--include", "*.pdf", "--seed", "1"])
    assert rc == 0
    assert png.read_bytes() == before


# --- --json output ---------------------------------------------------------

def test_cli_inject_json_output_shape(tree: Path, capsys):
    rc = main([
        "inject", str(tree), "--seed", "42",
        "--no-backup", "--json", "--intensity", "normal",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["seed"] == 42
    assert payload["haired"] == 2  # photo.png + sub/notes.pdf
    assert payload["errored"] == 0
    assert "aggregate" in payload
    assert payload["aggregate"]["hairs"] >= 1
    rels = {e["rel"] for e in payload["entries"]}
    assert "photo.png" in rels
    assert "sub/notes.pdf" in rels
    for entry in payload["entries"]:
        assert entry["status"] == "haired"
        assert "hairs" in entry


def test_cli_inject_json_dry_run(tree: Path, capsys):
    rc = main(["inject", str(tree), "--dry-run", "--json", "--seed", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["dry_run"] is True
    assert payload["seed"] == 5
    assert "photo.png" in payload["candidates"]


# --- --quiet / --verbose ---------------------------------------------------

def test_cli_inject_quiet_suppresses_per_file_lines(tree: Path, capsys):
    rc = main(["inject", str(tree), "--seed", "1", "--no-backup", "--quiet"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "haired  photo.png" not in out
    assert "haired  sub" not in out
    # Final summary still suppressed in quiet mode (it's an .info() call).
    assert out.strip() == ""


def test_cli_inject_verbose_includes_per_file_stats(tree: Path, capsys):
    rc = main(["inject", str(tree), "--seed", "1", "--no-backup", "--verbose"])
    assert rc == 0
    out = capsys.readouterr().out
    # Per-file lines include the parenthesised stats summary.
    assert "hair" in out and "(" in out and "content-aware" in out


# --- list ------------------------------------------------------------------

def test_cli_list_prints_categories(capsys):
    rc = main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "palettes:" in out
    assert "intensities:" in out
    assert "morphologies:" in out
    assert "file types:" in out
    # Sanity: one known value from each category appears.
    assert "white" in out
    assert "cousin-itt" in out
    assert "eyelash" in out
    assert ".zip" in out


# --- --version -------------------------------------------------------------

def test_cli_version_flag(capsys):
    with pytest.raises(SystemExit) as ei:
        main(["--version"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "strand" in out
    # Version follows PEP 440-ish — at minimum, a digit somewhere.
    assert any(ch.isdigit() for ch in out)


# --- undo ------------------------------------------------------------------

def test_cli_undo_finds_latest_manifest(tree: Path):
    original = (tree / "photo.png").read_bytes()
    rc = main(["inject", str(tree), "--seed", "1"])
    assert rc == 0
    assert (tree / "photo.png").read_bytes() != original

    # `undo <dir>` should find .strand_backups/manifest.json on its own.
    rc = main(["undo", str(tree)])
    assert rc == 0
    assert (tree / "photo.png").read_bytes() == original


def test_cli_undo_no_manifest_anywhere(tmp_path: Path, capsys):
    rc = main(["undo", str(tmp_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "no .strand_backups/manifest.json" in err


def test_cli_undo_accepts_explicit_manifest_path(tree: Path):
    original = (tree / "photo.png").read_bytes()
    main(["inject", str(tree), "--seed", "1"])
    manifest = tree / ".strand_backups" / "manifest.json"

    rc = main(["undo", str(manifest)])
    assert rc == 0
    assert (tree / "photo.png").read_bytes() == original


def test_cli_undo_picks_newest_when_multiple(tmp_path: Path):
    """If two backup runs exist under different subtrees, undo restores from the newer."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _make_png(a / "photo.png")
    _make_png(b / "photo.png")
    original_a = (a / "photo.png").read_bytes()
    original_b = (b / "photo.png").read_bytes()

    main(["inject", str(a), "--seed", "1"])
    # Bump mtime so b's manifest is unambiguously newer.
    time.sleep(0.05)
    main(["inject", str(b), "--seed", "2"])

    rc = main(["undo", str(tmp_path)])
    assert rc == 0
    # Only b's tree should have been restored.
    assert (b / "photo.png").read_bytes() == original_b
    # a is still stranded (we restored b, not a).
    assert (a / "photo.png").read_bytes() != original_a


# --- --yes / confirm bypass ------------------------------------------------

def test_cli_inject_yes_bypasses_confirm(tmp_path: Path):
    """A directory with > threshold candidates + --no-backup should run cleanly with --yes."""
    for i in range(12):
        _make_png(tmp_path / f"img{i:02d}.png")
    rc = main(["inject", str(tmp_path), "--no-backup", "--yes", "--seed", "1"])
    assert rc == 0
    # All files were processed.
    assert not (tmp_path / ".strand_backups").exists()


# --- --json stays clean (no ANSI) ------------------------------------------

def test_cli_inject_json_output_has_no_ansi(tree: Path, capsys):
    rc = main(["inject", str(tree), "--no-backup", "--json", "--seed", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    # No escape sequences should be present in JSON mode.
    assert "\x1b[" not in out
    # And it parses as JSON.
    json.loads(out)


# --- --version --------------------------------------------------------------

def test_cli_help_mentions_undo(capsys):
    """The top-level help text should list the `undo` subcommand."""
    with pytest.raises(SystemExit) as ei:
        main(["--help"])
    assert ei.value.code == 0
    out = capsys.readouterr().out
    assert "undo" in out

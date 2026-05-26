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
    assert manifest["palette"] == "dark"
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

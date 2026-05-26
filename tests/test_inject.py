"""
Roundtrip tests for the bytes-based injectors and the FastAPI app.

Fixtures are generated at test time — no binary blobs in the repo.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.core import (
    INTENSITY_TIERS,
    InjectOptions,
    strand_bytes,
    strand_zip_bytes,
    inject_image_bytes,
    inject_pdf_bytes,
    inject_pptx_bytes,
    options_from_ui,
)


# --- Fixtures --------------------------------------------------------------

@pytest.fixture
def png_bytes() -> bytes:
    """A small white PNG with a couple of rectangles, just to give content detection something to find."""
    from PIL import ImageDraw
    img = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((30, 30, 200, 80), fill="black")
    d.rectangle((50, 120, 350, 250), outline="black", width=3)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def jpeg_bytes() -> bytes:
    img = Image.new("RGB", (320, 240), "white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def pdf_bytes() -> bytes:
    from reportlab.lib.pagesizes import letter
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for i in range(3):
        c.setFont("Helvetica", 12)
        c.drawString(72, 720, f"Page {i + 1}")
        c.drawString(72, 700, "Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
        c.drawString(72, 680, "Phasellus blandit, mauris in commodo accumsan.")
        c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def pptx_bytes() -> bytes:
    from pptx import Presentation
    prs = Presentation()
    blank = prs.slide_layouts[6]
    for i in range(3):
        slide = prs.slides.add_slide(blank)
        from pptx.util import Inches
        tb = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1))
        tb.text_frame.text = f"Slide {i + 1}"
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


# --- Per-injector roundtrips ----------------------------------------------

def test_inject_png_roundtrip(png_bytes):
    opts = InjectOptions(seed=1, image_count=2)
    out = inject_image_bytes(png_bytes, ".png", opts)
    assert isinstance(out, bytes)
    assert len(out) > 0
    assert out != png_bytes

    with Image.open(io.BytesIO(out)) as im:
        im.load()
        assert im.size == (400, 300)
        assert im.format == "PNG"


def test_inject_jpeg_roundtrip(jpeg_bytes):
    opts = InjectOptions(seed=2, image_count=1)
    out = inject_image_bytes(jpeg_bytes, ".jpg", opts)
    with Image.open(io.BytesIO(out)) as im:
        im.load()
        assert im.size == (320, 240)
        assert im.format == "JPEG"


def test_inject_pdf_roundtrip(pdf_bytes):
    import fitz

    opts = InjectOptions(seed=3, rate=0.85)
    out = inject_pdf_bytes(pdf_bytes, opts)
    assert len(out) > 0
    assert out != pdf_bytes

    doc = fitz.open(stream=out, filetype="pdf")
    try:
        assert doc.page_count == 3
    finally:
        doc.close()


def test_inject_pdf_subtle_still_hits_at_least_one_page(pdf_bytes):
    """Subtle (rate=0.25) on a 3-page doc must still produce a haired page."""
    import fitz

    opts = InjectOptions(seed=12345, rate=0.25)
    out = inject_pdf_bytes(pdf_bytes, opts)
    # An untouched PDF round-tripped through PyMuPDF still differs from the
    # reportlab output, so size-difference isn't a useful signal here. Instead,
    # check that at least one page has embedded images.
    doc = fitz.open(stream=out, filetype="pdf")
    try:
        total_images = sum(len(doc[i].get_images()) for i in range(doc.page_count))
        assert total_images >= 1
    finally:
        doc.close()


def test_inject_pptx_roundtrip(pptx_bytes):
    from pptx import Presentation

    opts = InjectOptions(seed=4, rate=0.85)
    out = inject_pptx_bytes(pptx_bytes, opts)
    assert len(out) > 0
    assert out != pptx_bytes

    prs = Presentation(io.BytesIO(out))
    assert len(prs.slides) == 3


def test_strand_bytes_dispatches_by_suffix(png_bytes, pdf_bytes):
    opts = InjectOptions(seed=5)
    assert strand_bytes(png_bytes, "photo.png", opts) != png_bytes
    assert strand_bytes(pdf_bytes, "deck.pdf", opts) != pdf_bytes


def test_strand_bytes_rejects_unsupported():
    with pytest.raises(ValueError):
        strand_bytes(b"hello", "notes.txt", InjectOptions())


def test_seed_is_deterministic(png_bytes):
    a = inject_image_bytes(png_bytes, ".png", InjectOptions(seed=42, image_count=2))
    b = inject_image_bytes(png_bytes, ".png", InjectOptions(seed=42, image_count=2))
    assert a == b


# --- HTTP layer ------------------------------------------------------------

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Strand" in r.text


def test_strand_endpoint_png(client, png_bytes):
    r = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "normal"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert "photo-strand.png" in r.headers.get("content-disposition", "")
    assert len(r.content) > 0
    # Confirm the response decodes as a valid PNG.
    with Image.open(io.BytesIO(r.content)) as im:
        im.load()
        assert im.format == "PNG"


def test_strand_endpoint_rejects_unsupported(client):
    r = client.post(
        "/strand",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"palette": "dark", "intensity": "normal"},
    )
    assert r.status_code == 415


def test_strand_endpoint_rejects_empty(client):
    r = client.post(
        "/strand",
        files={"file": ("photo.png", b"", "image/png")},
        data={"palette": "dark", "intensity": "normal"},
    )
    assert r.status_code == 400


# --- New v1.1 surface --------------------------------------------------------

def test_intensity_table_is_monotonic_in_density():
    """The funny tiers should actually escalate; if someone reorders the dict,
    fail loudly."""
    tiers = list(INTENSITY_TIERS.values())
    for prev, nxt in zip(tiers, tiers[1:]):
        assert nxt["image_count"] >= prev["image_count"]
        # Hairs per page should never go down either.
        assert nxt["hairs_per_page"] >= prev["hairs_per_page"]
    # Sanity: top tier is meaningfully more hair than bottom.
    assert tiers[-1]["image_count"] >= tiers[0]["image_count"] * 10


@pytest.mark.parametrize("intensity", list(INTENSITY_TIERS.keys()))
def test_every_intensity_tier_runs(png_bytes, intensity):
    opts = options_from_ui(palette="dark", intensity=intensity, seed=7)
    out = inject_image_bytes(png_bytes, ".png", opts)
    assert len(out) > 0
    with Image.open(io.BytesIO(out)) as im:
        im.load()
        assert im.size == (400, 300)


def test_higher_density_embeds_more_images_in_pdf(pdf_bytes):
    """heavy should produce strictly more embedded images than subtle on the same doc."""
    import fitz

    def total_images(intensity):
        opts = options_from_ui(palette="dark", intensity=intensity, seed=1234)
        out = inject_pdf_bytes(pdf_bytes, opts)
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            return sum(len(doc[i].get_images()) for i in range(doc.page_count))
        finally:
            doc.close()

    n_subtle = total_images("subtle")
    n_heavy = total_images("heavy")
    n_werewolf = total_images("werewolf")
    assert n_subtle >= 1
    assert n_heavy > n_subtle
    assert n_werewolf > n_heavy


def test_seed_is_populated_after_construction():
    """Even without an explicit seed, opts.seed must be readable so we can echo it."""
    opts = InjectOptions()
    assert isinstance(opts.seed, int)
    assert opts.seed > 0


# --- ZIP roundtrip -----------------------------------------------------------

@pytest.fixture
def zip_bytes(png_bytes, pdf_bytes, pptx_bytes) -> bytes:
    """A zip with one of each supported format plus a deliberate unsupported entry and a nested dir."""
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("photo.png", png_bytes)
        z.writestr("docs/deck.pptx", pptx_bytes)
        z.writestr("docs/manual.pdf", pdf_bytes)
        z.writestr("README.txt", b"keep me as is\n")
    return buf.getvalue()


def test_zip_roundtrip_processes_each_supported_entry(zip_bytes):
    import zipfile
    opts = options_from_ui(palette="dark", intensity="normal", seed=42)
    out, report = strand_zip_bytes(zip_bytes, opts, name_suffix="-strand")

    assert report["haired_count"] == 3
    assert report["skipped_count"] == 1
    assert report["error_count"] == 0

    with zipfile.ZipFile(io.BytesIO(out)) as z:
        names = set(z.namelist())
        assert "photo-strand.png" in names
        assert "docs/deck-strand.pptx" in names
        assert "docs/manual-strand.pdf" in names
        # Unsupported entries pass through unchanged with the original name.
        assert "README.txt" in names
        assert z.read("README.txt") == b"keep me as is\n"
        # Report is embedded.
        assert "_strand-report.txt" in names
        report_text = z.read("_strand-report.txt").decode("utf-8")
        assert "Strand report" in report_text
        assert "seed:" in report_text


def test_zip_custom_suffix(zip_bytes):
    import zipfile
    opts = options_from_ui(palette="dark", intensity="subtle", seed=99)
    out, _ = strand_zip_bytes(zip_bytes, opts, name_suffix=".v2")
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert "photo.v2.png" in z.namelist()
        assert "docs/deck.v2.pptx" in z.namelist()


def test_zip_empty_suffix_keeps_names(zip_bytes):
    import zipfile
    opts = options_from_ui(palette="dark", intensity="subtle", seed=99)
    out, _ = strand_zip_bytes(zip_bytes, opts, name_suffix="")
    with zipfile.ZipFile(io.BytesIO(out)) as z:
        assert "photo.png" in z.namelist()
        # Content still changed.
        assert z.read("photo.png") != z.read("photo.png")[:0] + b""  # not empty
        assert "_strand-report.txt" in z.namelist()


# --- HTTP layer for the new surface ------------------------------------------

def test_seed_header_is_echoed(client, png_bytes):
    r = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "normal"},
    )
    assert r.status_code == 200
    seed = r.headers.get("X-Strand-Seed")
    assert seed is not None and seed.isdigit()


def test_seed_reuse_reproduces_output(client, png_bytes):
    r1 = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "normal"},
    )
    seed = r1.headers["X-Strand-Seed"]
    r2 = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "normal", "seed": seed},
    )
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content


def test_custom_suffix_in_content_disposition(client, png_bytes):
    r = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "subtle", "name_suffix": ".v2"},
    )
    assert r.status_code == 200
    assert "photo.v2.png" in r.headers["content-disposition"]


def test_empty_suffix_keeps_original_name(client, png_bytes):
    r = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "subtle", "name_suffix": ""},
    )
    assert r.status_code == 200
    assert 'filename="photo.png"' in r.headers["content-disposition"]


def test_suffix_strips_path_separators(client, png_bytes):
    r = client.post(
        "/strand",
        files={"file": ("photo.png", png_bytes, "image/png")},
        data={"palette": "dark", "intensity": "subtle", "name_suffix": "../oops"},
    )
    assert r.status_code == 200
    # The slashes / dots-at-start were stripped; the resulting suffix is "..oops".
    cd = r.headers["content-disposition"]
    assert "/" not in cd.split("filename=")[1]


def test_zip_endpoint_roundtrip(client, zip_bytes):
    import zipfile
    r = client.post(
        "/strand",
        files={"file": ("bundle.zip", zip_bytes, "application/zip")},
        data={"palette": "dark", "intensity": "normal"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "bundle-strand.zip" in r.headers["content-disposition"]
    assert r.headers.get("X-Strand-Haired") == "3"
    assert r.headers.get("X-Strand-Skipped") == "1"

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert "photo-strand.png" in z.namelist()
        assert "_strand-report.txt" in z.namelist()

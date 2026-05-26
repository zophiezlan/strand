"""
Core hair rendering and bytes-based injectors for Strand.

Shared by the FastAPI web service (`app.main`) and the CLI (`app.cli`). The
public API is:

    InjectOptions       — request-level knobs
    strand_bytes(...)   — dispatch on filename / content type
    strand_zip_bytes(...) — walk a zip, strand each supported entry
    inject_image_bytes(...), inject_pdf_bytes(...), inject_pptx_bytes(...)

Everything operates on bytes / BytesIO. Nothing in this module touches the disk.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass, field

from PIL import Image, ImageDraw


# Supersample at 4x and downsample for smooth strand edges.
_SCALE = 4
_PAD = 12


def _cubic_bezier(t, p0, p1, p2, p3):
    mt = 1 - t
    x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
    y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
    return (x, y)


def _new_canvas(base_width, base_height):
    cw = (base_width + 2 * _PAD) * _SCALE
    ch = (base_height + 2 * _PAD) * _SCALE
    img = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img), cw, ch


def _to_canvas(x, y):
    return ((_PAD + x) * _SCALE, (_PAD + y) * _SCALE)


PALETTES = {
    "dark":    ((10, 6, 4),     (45, 32, 28)),
    "brown":   ((45, 30, 18),   (95, 65, 38)),
    "blonde":  ((130, 100, 55), (210, 180, 130)),
    "grey":    ((90, 88, 85),   (170, 168, 165)),
    "white":   ((200, 198, 195),(235, 233, 230)),
    "red":     ((90, 38, 18),   (165, 80, 42)),
}
PALETTE_NAMES = set(PALETTES) | {"mixed"}


def _random_hair_color(rng, palette="dark"):
    if palette == "mixed":
        palette = rng.choice(list(PALETTES))
    lo, hi = PALETTES[palette]
    return (rng.randint(lo[0], hi[0]),
            rng.randint(lo[1], hi[1]),
            rng.randint(lo[2], hi[2]))


def _draw_bezier_segment(draw, p0, p1, p2, p3, base_color, rng,
                         n_samples=220, width_start=1.9, width_end=1.0):
    points = [_cubic_bezier(i / (n_samples - 1), p0, p1, p2, p3) for i in range(n_samples)]
    for i in range(len(points) - 1):
        t = i / (n_samples - 1)
        w = (width_start - t * (width_start - width_end) + rng.uniform(-0.15, 0.15)) * _SCALE
        w = max(_SCALE * 0.5, w)

        r = max(0, min(255, base_color[0] + rng.randint(-8, 8)))
        g = max(0, min(255, base_color[1] + rng.randint(-6, 6)))
        b = max(0, min(255, base_color[2] + rng.randint(-5, 5)))
        a = rng.randint(220, 250)

        draw.line([points[i], points[i + 1]], fill=(r, g, b, a), width=int(round(w)))


def _finalize(img, cw, ch):
    return img.resize((cw // _SCALE, ch // _SCALE), Image.LANCZOS)


def _generate_curve_hair(rng, palette="dark", base_width=400, base_height=120):
    img, draw, cw, ch = _new_canvas(base_width, base_height)

    p0 = _to_canvas(
        rng.uniform(0, base_width * 0.05),
        base_height * 0.5 + rng.uniform(-base_height * 0.2, base_height * 0.2),
    )
    p3 = _to_canvas(
        base_width * 0.95 + rng.uniform(-base_width * 0.05, base_width * 0.03),
        base_height * 0.5 + rng.uniform(-base_height * 0.3, base_height * 0.3),
    )
    p1 = _to_canvas(
        base_width * 0.3 + rng.uniform(-base_width * 0.1, base_width * 0.1),
        base_height * 0.5 + rng.uniform(-base_height * 0.7, base_height * 0.7),
    )
    p2 = _to_canvas(
        base_width * 0.7 + rng.uniform(-base_width * 0.1, base_width * 0.1),
        base_height * 0.5 + rng.uniform(-base_height * 0.7, base_height * 0.7),
    )

    base_color = _random_hair_color(rng, palette)
    _draw_bezier_segment(draw, p0, p1, p2, p3, base_color, rng,
                         n_samples=220, width_start=1.9, width_end=1.0)
    return _finalize(img, cw, ch)


def _generate_loop_hair(rng, palette="dark", base_width=400, base_height=240):
    img, draw, cw, ch = _new_canvas(base_width, base_height)

    loop_cx = base_width * rng.uniform(0.25, 0.38)
    loop_cy = base_height * 0.5
    loop_r = min(base_width, base_height) * rng.uniform(0.22, 0.32)

    theta_close = rng.uniform(-0.35, 0.35)
    theta_open = theta_close + rng.uniform(0.18, 0.45)

    a_local = (loop_cx + loop_r * math.cos(theta_close),
               loop_cy + loop_r * math.sin(theta_close))
    b_local = (loop_cx + loop_r * math.cos(theta_open),
               loop_cy + loop_r * math.sin(theta_open))

    tan_a = (-math.sin(theta_close), math.cos(theta_close))
    tan_b = (math.sin(theta_open), -math.cos(theta_open))

    # ~1.7r gives a near-circular Bezier loop
    k = loop_r * 1.7
    p1_local = (a_local[0] + k * tan_a[0], a_local[1] + k * tan_a[1])
    p2_local = (b_local[0] + k * tan_b[0], b_local[1] + k * tan_b[1])

    base_color = _random_hair_color(rng, palette)
    _draw_bezier_segment(
        draw,
        _to_canvas(*a_local), _to_canvas(*p1_local),
        _to_canvas(*p2_local), _to_canvas(*b_local),
        base_color, rng,
        n_samples=200, width_start=1.4, width_end=1.4,
    )

    exit_dx = b_local[0] - p2_local[0]
    exit_dy = b_local[1] - p2_local[1]
    mag = math.hypot(exit_dx, exit_dy) or 1.0
    tail_dir = (exit_dx / mag, exit_dy / mag)

    tail_len = base_width * rng.uniform(0.40, 0.55)
    t_end_local = (b_local[0] + tail_len * tail_dir[0] + rng.uniform(-15, 15),
                   b_local[1] + tail_len * tail_dir[1] + rng.uniform(-25, 25))
    t_p1_local = (b_local[0] + tail_len * 0.3 * tail_dir[0] + rng.uniform(-10, 10),
                  b_local[1] + tail_len * 0.3 * tail_dir[1] + rng.uniform(-12, 12))
    t_p2_local = (b_local[0] + tail_len * 0.7 * tail_dir[0] + rng.uniform(-15, 15),
                  b_local[1] + tail_len * 0.7 * tail_dir[1] + rng.uniform(-18, 18))

    _draw_bezier_segment(
        draw,
        _to_canvas(*b_local), _to_canvas(*t_p1_local),
        _to_canvas(*t_p2_local), _to_canvas(*t_end_local),
        base_color, rng,
        n_samples=140, width_start=1.4, width_end=0.9,
    )

    return _finalize(img, cw, ch)


def _generate_eyelash_hair(rng: random.Random, palette: str = "dark",
                           base_width: int = 400, base_height: int = 120) -> Image.Image:
    """A short, tightly curved hair — eyelash-like.

    Drawn in a small region of a full-sized canvas, so when `_place` scales the
    final image, an eyelash naturally ends up smaller on the page than a curve
    or loop. (The transparent surround does the work.)
    """
    img, draw, cw, ch = _new_canvas(base_width, base_height)

    cx = base_width * 0.5
    cy = base_height * 0.5

    span = rng.uniform(28, 50)
    arc = rng.uniform(18, 32) * rng.choice([-1, 1])

    p0 = _to_canvas(cx - span / 2, cy + rng.uniform(-2, 2))
    p3 = _to_canvas(cx + span / 2, cy + rng.uniform(-2, 2))
    p1 = _to_canvas(cx - span * 0.18, cy + arc)
    p2 = _to_canvas(cx + span * 0.18, cy + arc * 0.85)

    base_color = _random_hair_color(rng, palette)
    _draw_bezier_segment(draw, p0, p1, p2, p3, base_color, rng,
                         n_samples=120, width_start=1.9, width_end=1.0)
    return _finalize(img, cw, ch)


def _generate_fragment_hair(rng: random.Random, palette: str = "dark",
                            base_width: int = 400, base_height: int = 120) -> Image.Image:
    """A short broken piece of hair — fragmentary, less curved than an eyelash."""
    img, draw, cw, ch = _new_canvas(base_width, base_height)

    cx = base_width * 0.5
    cy = base_height * 0.5

    span = rng.uniform(45, 90)
    bend = rng.uniform(-12, 12)

    p0 = _to_canvas(cx - span / 2 + rng.uniform(-3, 3), cy + rng.uniform(-3, 3))
    p3 = _to_canvas(cx + span / 2 + rng.uniform(-3, 3), cy + rng.uniform(-5, 5))
    p1 = _to_canvas(cx - span * 0.2, cy + bend)
    p2 = _to_canvas(cx + span * 0.2, cy + bend * 0.6 + rng.uniform(-4, 4))

    base_color = _random_hair_color(rng, palette)
    # Thinner than a full strand — fragments are wispy.
    _draw_bezier_segment(draw, p0, p1, p2, p3, base_color, rng,
                         n_samples=110, width_start=1.4, width_end=0.7)
    return _finalize(img, cw, ch)


# Public dispatch table — used by /api/sample to render a specific morphology
# and by `generate_hair` for weighted random selection.
MORPHOLOGIES: dict[str, "callable"] = {
    "curve":    _generate_curve_hair,
    "loop":     _generate_loop_hair,
    "eyelash":  _generate_eyelash_hair,
    "fragment": _generate_fragment_hair,
}


def generate_hair_with_morphology(
    rng: random.Random,
    palette: str = "dark",
    loop_chance: float = 0.15,
    eyelash_chance: float = 0.08,
    fragment_chance: float = 0.08,
) -> tuple[Image.Image, str, str]:
    """Pick a morphology by weighted random choice; return (image, morphology, palette).

    For "mixed" palette, a concrete palette is resolved once per hair (so one
    strand stays in a single colour family) and the resolved name is returned
    so callers can build a colour breakdown.
    """
    r = rng.random()
    # Morphology pick happens first so the RNG sequence is unchanged when
    # palette isn't "mixed" (preserves the seed-determinism guarantee).
    if r < loop_chance:
        morph = "loop"
        renderer = _generate_loop_hair
    elif r < loop_chance + eyelash_chance:
        morph = "eyelash"
        renderer = _generate_eyelash_hair
    elif r < loop_chance + eyelash_chance + fragment_chance:
        morph = "fragment"
        renderer = _generate_fragment_hair
    else:
        morph = "curve"
        renderer = _generate_curve_hair

    # Resolve "mixed" → concrete palette here (rather than inside
    # _random_hair_color) so we can report which colour family was drawn.
    resolved = palette
    if resolved == "mixed":
        resolved = rng.choice(list(PALETTES))

    return renderer(rng, palette=resolved), morph, resolved


def generate_hair(
    rng: random.Random,
    palette: str = "dark",
    loop_chance: float = 0.15,
    eyelash_chance: float = 0.08,
    fragment_chance: float = 0.08,
) -> Image.Image:
    """Pick a morphology by weighted random choice; default is the curved strand."""
    img, _, _ = generate_hair_with_morphology(
        rng, palette=palette,
        loop_chance=loop_chance, eyelash_chance=eyelash_chance,
        fragment_chance=fragment_chance,
    )
    return img


def _empty_stats() -> dict:
    """Fresh stats dict — every counter the injectors fill in.

    `palettes` and `morphologies` are dynamic maps of name → count.
    `clusters` counts primary hairs that grew at least one buddy.
    """
    return {
        "hairs": 0,
        "morphologies": {"curve": 0, "loop": 0, "eyelash": 0, "fragment": 0},
        "palettes": {},
        "pages_touched": 0,
        "clusters": 0,
    }


def _bump(d: dict, key: str) -> None:
    d[key] = d.get(key, 0) + 1


def _merge_stats(into: dict, src: dict) -> None:
    """Aggregate `src` stats into `into` in place. Used by the zip handler."""
    into["hairs"] += src.get("hairs", 0)
    into["pages_touched"] += src.get("pages_touched", 0)
    into["clusters"] += src.get("clusters", 0)
    for m, n in src.get("morphologies", {}).items():
        into["morphologies"][m] = into["morphologies"].get(m, 0) + n
    for p, n in src.get("palettes", {}).items():
        into["palettes"][p] = into["palettes"].get(p, 0) + n


def _rotate_hair(hair: Image.Image, rng: random.Random) -> Image.Image:
    angle = rng.uniform(0, 360)
    return hair.rotate(angle, expand=True, resample=Image.BICUBIC)


# ---------------------------------------------------------------------------
# Content region detection — bias placement toward areas with real content.
# ---------------------------------------------------------------------------

def _image_content_regions(img: Image.Image, grid: int = 10, threshold: float = 3.0):
    from PIL import ImageFilter, ImageStat
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    w, h = edges.size
    cw = max(1, w // grid)
    ch = max(1, h // grid)
    regions = []
    for cy in range(grid):
        for cx in range(grid):
            x0 = cx * cw
            y0 = cy * ch
            x1 = x0 + cw
            y1 = y0 + ch
            cell = edges.crop((x0, y0, x1, y1))
            mean = ImageStat.Stat(cell).mean[0]
            if mean >= threshold:
                regions.append((x0, y0, x1, y1, mean))
    return regions


def _pdf_content_regions(page):
    regions = []
    try:
        blocks = page.get_text("blocks")
    except Exception:
        return regions
    for b in blocks:
        if len(b) < 4:
            continue
        x0, y0, x1, y1 = b[:4]
        area = max(1.0, (x1 - x0) * (y1 - y0))
        regions.append((x0, y0, x1, y1, area))
    return regions


def _pptx_content_regions(slide):
    regions = []
    for shape in slide.shapes:
        if (shape.left is None or shape.top is None or
                shape.width is None or shape.height is None):
            continue
        x0 = shape.left
        y0 = shape.top
        x1 = x0 + shape.width
        y1 = y0 + shape.height
        area = max(1.0, shape.width * shape.height)
        regions.append((x0, y0, x1, y1, area))
    return regions


def _place(rng, container_w, container_h, hair_w, hair_h, scale_range,
           regions=None, content_bias: float = 0.5):
    target_long_frac = rng.uniform(*scale_range)
    short_side = min(container_w, container_h)
    target_long = short_side * target_long_frac
    scale = target_long / max(hair_w, hair_h)
    dw = hair_w * scale
    dh = hair_h * scale

    use_region = bool(regions) and rng.random() < content_bias

    if use_region:
        weights = [r[4] for r in regions]
        region = rng.choices(regions, weights=weights, k=1)[0]
        rx0, ry0, rx1, ry1 = region[:4]
        rcx = (rx0 + rx1) / 2
        rcy = (ry0 + ry1) / 2
        jitter_x = max((rx1 - rx0) * 0.6, dw * 0.2)
        jitter_y = max((ry1 - ry0) * 0.6, dh * 0.2)
        x = rcx - dw / 2 + rng.uniform(-jitter_x, jitter_x)
        y = rcy - dh / 2 + rng.uniform(-jitter_y, jitter_y)
    else:
        max_x = max(0.0, container_w - dw)
        max_y = max(0.0, container_h - dh)
        x = rng.triangular(0, max_x, max_x / 2) if max_x > 0 else 0
        y = rng.triangular(0, max_y, max_y / 2) if max_y > 0 else 0

    x = max(0, min(container_w - dw, x))
    y = max(0, min(container_h - dh, y))
    return x, y, dw, dh


def _morph_kwargs(opts) -> dict:
    """Bundle morphology weights for a `generate_hair` call."""
    return dict(
        palette=opts.palette,
        loop_chance=opts.loop_chance,
        eyelash_chance=opts.eyelash_chance,
        fragment_chance=opts.fragment_chance,
    )


def _buddy_offsets(rng, dw, dh, container_w, container_h, base_x, base_y,
                   cluster_chance, max_buddies=2):
    """
    Roll up to `max_buddies` times for a buddy hair near (base_x, base_y).

    Each roll succeeds with probability `cluster_chance`. A successful roll
    yields an (x, y) offset within ~0.6× hair size of the seed position,
    clamped to the container bounds. Lets real-world clumping show up
    without requiring every hair to clump.
    """
    out = []
    for _ in range(max_buddies):
        if rng.random() >= cluster_chance:
            break
        ox = base_x + rng.uniform(-dw * 0.6, dw * 0.6)
        oy = base_y + rng.uniform(-dh * 0.6, dh * 0.6)
        ox = max(0, min(container_w - dw, ox))
        oy = max(0, min(container_h - dh, oy))
        out.append((ox, oy))
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
PDF_SUFFIXES = {".pdf"}
PPTX_SUFFIXES = {".pptx"}
ZIP_SUFFIXES = {".zip"}
SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES | PPTX_SUFFIXES
SUPPORTED_INCLUDING_ZIP = SUPPORTED_SUFFIXES | ZIP_SUFFIXES


# Ordered intensity tiers. Past "heavy" the labels are the joke — every page
# gets multiple hairs, every image gets a fistful.
INTENSITY_TIERS: dict[str, dict] = {
    "subtle":     {"label": "Subtle",     "image_count": 1,  "rate": 0.25, "hairs_per_page": 1},
    "normal":     {"label": "Normal",     "image_count": 2,  "rate": 0.50, "hairs_per_page": 1},
    "heavy":      {"label": "Heavy",      "image_count": 4,  "rate": 0.85, "hairs_per_page": 2},
    "hirsute":    {"label": "Hirsute",    "image_count": 10, "rate": 1.0,  "hairs_per_page": 4},
    "werewolf":   {"label": "Werewolf",   "image_count": 25, "rate": 1.0,  "hairs_per_page": 9},
    "cousin-itt": {"label": "Cousin Itt", "image_count": 60, "rate": 1.0,  "hairs_per_page": 22},
}
INTENSITY_ORDER: list[str] = list(INTENSITY_TIERS.keys())
# Default tuned by real-world testing: a single light hair best mimics the
# photocopier-glass / laminator-pocket artefact, which reads most convincingly
# as a "real" stray hair. Heavier tiers exist for play, not for first impressions.
DEFAULT_INTENSITY = "subtle"
DEFAULT_PALETTE = "white"


@dataclass
class InjectOptions:
    """Request-level knobs for a single strand call."""
    # Per-page/slide probability for PDFs and pptx. For images, see image_count.
    rate: float = 0.5
    # Hairs per *selected* PDF page or pptx slide.
    hairs_per_page: int = 1
    # Hairs to overlay on a single image. PDFs/pptx use rate + hairs_per_page.
    image_count: int = 2
    palette: str = "white"
    content_bias: float = 0.5
    scale_range: tuple[float, float] = (0.15, 0.45)
    # Morphology weights — what fraction of hairs are loops / eyelashes /
    # fragments (the rest are the default curved strand). Defaults give a
    # visible-but-not-dominant mix of variety.
    loop_chance: float = 0.15
    eyelash_chance: float = 0.08
    fragment_chance: float = 0.08
    # Probability that an already-placed hair gets a "buddy" placed nearby,
    # rolled repeatedly per hair (so a single hair can be the head of a small
    # tuft of 1–3). Real shed hair clumps; even spread looks artificial.
    cluster_chance: float = 0.22
    # Always populated after construction (see __post_init__) so callers can
    # echo the seed back to the user for "re-strand with this seed".
    seed: int = field(default_factory=lambda: random.randrange(1, 2**31 - 1))

    def __post_init__(self):
        if self.seed is None:  # accept None defensively
            self.seed = random.randrange(1, 2**31 - 1)

    def rng(self) -> random.Random:
        return random.Random(self.seed)


def _normalize_intensity(intensity: str) -> str:
    raw = (intensity or "").lower().strip()
    if raw not in INTENSITY_TIERS:
        return DEFAULT_INTENSITY
    return raw


def options_from_ui(palette: str, intensity: str, seed: int | None = None) -> InjectOptions:
    """Translate the UI's coarse choices into InjectOptions."""
    palette = (palette or "").lower().strip()
    if palette not in PALETTE_NAMES:
        palette = DEFAULT_PALETTE
    tier = INTENSITY_TIERS[_normalize_intensity(intensity)]
    kwargs = dict(
        rate=tier["rate"],
        image_count=tier["image_count"],
        hairs_per_page=tier["hairs_per_page"],
        palette=palette,
    )
    if seed is not None:
        kwargs["seed"] = seed
    return InjectOptions(**kwargs)


def inject_image_bytes(data: bytes, suffix: str, opts: InjectOptions) -> tuple[bytes, dict]:
    """Overlay hairs onto an image. Returns (encoded bytes, stats dict)."""
    suffix = suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        raise ValueError(f"unsupported image suffix: {suffix}")

    rng = opts.rng()
    stats = _empty_stats()
    stats["pages_touched"] = 1
    with Image.open(io.BytesIO(data)) as src:
        img = src.convert("RGBA")

    iw, ih = img.size
    regions = _image_content_regions(img)

    for _ in range(max(1, opts.image_count)):
        hair, morph, color = generate_hair_with_morphology(rng, **_morph_kwargs(opts))
        hair = _rotate_hair(hair, rng)
        stats["hairs"] += 1
        stats["morphologies"][morph] += 1
        _bump(stats["palettes"], color)
        hw, hh = hair.size
        x, y, dw, dh = _place(rng, iw, ih, hw, hh, opts.scale_range,
                              regions=regions, content_bias=opts.content_bias)
        hair_sized = hair.resize((max(1, int(dw)), max(1, int(dh))), Image.LANCZOS)
        img.alpha_composite(hair_sized, (int(x), int(y)))

        buddy_count = 0
        for bx, by in _buddy_offsets(rng, dw, dh, iw, ih, x, y, opts.cluster_chance):
            buddy, bmorph, bcolor = generate_hair_with_morphology(rng, **_morph_kwargs(opts))
            buddy = _rotate_hair(buddy, rng)
            stats["hairs"] += 1
            stats["morphologies"][bmorph] += 1
            _bump(stats["palettes"], bcolor)
            buddy_count += 1
            scale = rng.uniform(0.7, 1.0)
            bw, bh = max(1, int(dw * scale)), max(1, int(dh * scale))
            buddy = buddy.resize((bw, bh), Image.LANCZOS)
            img.alpha_composite(buddy, (int(bx), int(by)))
        if buddy_count > 0:
            stats["clusters"] += 1

    out = io.BytesIO()
    if suffix in (".jpg", ".jpeg"):
        img.convert("RGB").save(out, format="JPEG", quality=92, optimize=True)
    elif suffix == ".gif":
        img.convert("P", palette=Image.ADAPTIVE).save(out, format="GIF")
    elif suffix == ".bmp":
        img.convert("RGB").save(out, format="BMP")
    elif suffix == ".webp":
        img.save(out, format="WEBP", quality=92)
    else:
        img.save(out, format="PNG")
    return out.getvalue(), stats


def inject_pdf_bytes(data: bytes, opts: InjectOptions) -> tuple[bytes, dict]:
    """Overlay hairs onto a PDF. Returns (rewritten bytes, stats dict)."""
    import fitz  # PyMuPDF

    rng = opts.rng()
    stats = _empty_stats()
    doc = fitz.open(stream=data, filetype="pdf")

    try:
        page_count = doc.page_count
        if page_count == 0:
            return data, stats

        # Pick which pages get hair. Per-page Bernoulli with at-least-one guarantee
        # so a 2-page doc on subtle still gets visibly haired.
        hit = [rng.random() < opts.rate for _ in range(page_count)]
        if not any(hit):
            hit[rng.randrange(page_count)] = True

        per_page = max(1, opts.hairs_per_page)
        for i, do_it in enumerate(hit):
            if not do_it:
                continue
            stats["pages_touched"] += 1
            page = doc[i]
            regions = _pdf_content_regions(page)
            pw = page.rect.width
            ph = page.rect.height
            for _ in range(per_page):
                hair, morph, color = generate_hair_with_morphology(rng, **_morph_kwargs(opts))
                hair = _rotate_hair(hair, rng)
                stats["hairs"] += 1
                stats["morphologies"][morph] += 1
                _bump(stats["palettes"], color)
                hw, hh = hair.size
                x, y, dw, dh = _place(rng, pw, ph, hw, hh, opts.scale_range,
                                      regions=regions, content_bias=opts.content_bias)

                buf = io.BytesIO()
                hair.save(buf, format="PNG")
                hair_png = buf.getvalue()
                page.insert_image(
                    fitz.Rect(x, y, x + dw, y + dh),
                    stream=hair_png,
                    keep_proportion=False,
                    overlay=True,
                )

                buddy_count = 0
                for bx, by in _buddy_offsets(rng, dw, dh, pw, ph, x, y, opts.cluster_chance):
                    buddy, bmorph, bcolor = generate_hair_with_morphology(rng, **_morph_kwargs(opts))
                    buddy = _rotate_hair(buddy, rng)
                    stats["hairs"] += 1
                    stats["morphologies"][bmorph] += 1
                    _bump(stats["palettes"], bcolor)
                    buddy_count += 1
                    bs = rng.uniform(0.7, 1.0)
                    bw_, bh_ = dw * bs, dh * bs
                    bbuf = io.BytesIO()
                    buddy.save(bbuf, format="PNG")
                    page.insert_image(
                        fitz.Rect(bx, by, bx + bw_, by + bh_),
                        stream=bbuf.getvalue(),
                        keep_proportion=False,
                        overlay=True,
                    )
                if buddy_count > 0:
                    stats["clusters"] += 1

        return doc.tobytes(garbage=4, deflate=True), stats
    finally:
        doc.close()


def inject_pptx_bytes(data: bytes, opts: InjectOptions) -> tuple[bytes, dict]:
    """Overlay hairs onto a pptx. Returns (rewritten bytes, stats dict)."""
    from pptx import Presentation

    rng = opts.rng()
    stats = _empty_stats()
    prs = Presentation(io.BytesIO(data))
    sw, sh = prs.slide_width, prs.slide_height  # EMUs

    slides = list(prs.slides)
    if not slides:
        out = io.BytesIO()
        prs.save(out)
        return out.getvalue(), stats

    hit = [rng.random() < opts.rate for _ in slides]
    if not any(hit):
        hit[rng.randrange(len(slides))] = True

    per_slide = max(1, opts.hairs_per_page)
    for slide, do_it in zip(slides, hit):
        if not do_it:
            continue
        stats["pages_touched"] += 1
        regions = _pptx_content_regions(slide)
        for _ in range(per_slide):
            hair, morph, color = generate_hair_with_morphology(rng, **_morph_kwargs(opts))
            hair = _rotate_hair(hair, rng)
            stats["hairs"] += 1
            stats["morphologies"][morph] += 1
            _bump(stats["palettes"], color)
            hw, hh = hair.size
            x, y, dw, dh = _place(rng, sw, sh, hw, hh, opts.scale_range,
                                  regions=regions, content_bias=opts.content_bias)

            buf = io.BytesIO()
            hair.save(buf, format="PNG")
            buf.seek(0)
            slide.shapes.add_picture(buf, int(x), int(y), width=int(dw), height=int(dh))

            buddy_count = 0
            for bx, by in _buddy_offsets(rng, dw, dh, sw, sh, x, y, opts.cluster_chance):
                buddy, bmorph, bcolor = generate_hair_with_morphology(rng, **_morph_kwargs(opts))
                buddy = _rotate_hair(buddy, rng)
                stats["hairs"] += 1
                stats["morphologies"][bmorph] += 1
                _bump(stats["palettes"], bcolor)
                buddy_count += 1
                bs = rng.uniform(0.7, 1.0)
                bbuf = io.BytesIO()
                buddy.save(bbuf, format="PNG")
                bbuf.seek(0)
                slide.shapes.add_picture(
                    bbuf, int(bx), int(by),
                    width=int(dw * bs), height=int(dh * bs),
                )
            if buddy_count > 0:
                stats["clusters"] += 1

    out = io.BytesIO()
    prs.save(out)
    return out.getvalue(), stats


def _suffix_of(name: str) -> str:
    return ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""


def strand_bytes(data: bytes, filename: str, opts: InjectOptions) -> tuple[bytes, dict]:
    """Dispatch to the right injector based on filename suffix.

    Returns (rewritten bytes, stats dict). Stats include total hair count,
    a per-morphology breakdown, and pages touched.
    """
    suffix = _suffix_of(filename)
    if suffix in IMAGE_SUFFIXES:
        return inject_image_bytes(data, suffix, opts)
    if suffix in PDF_SUFFIXES:
        return inject_pdf_bytes(data, opts)
    if suffix in PPTX_SUFFIXES:
        return inject_pptx_bytes(data, opts)
    raise ValueError(f"unsupported file type: {suffix or filename!r}")


def _apply_suffix(name: str, suffix: str) -> str:
    """Insert `suffix` between stem and extension. Empty suffix is a no-op."""
    if not suffix:
        return name
    if "." in name:
        stem, _, ext = name.rpartition(".")
        return f"{stem}{suffix}.{ext}"
    return f"{name}{suffix}"


def strand_zip_bytes(
    data: bytes,
    opts: InjectOptions,
    name_suffix: str = "-strand",
) -> tuple[bytes, dict]:
    """
    Walk every entry in a zip, strand supported files, return (zip_bytes, report).

    Each entry's status is one of: "hairified", "skipped", "errored". A
    `_strand-report.txt` is added to the output zip alongside the entries.
    Directory entries are preserved as-is.
    """
    import zipfile

    base_seed = opts.seed
    report = {
        "entries": [],
        "haired_count": 0,
        "skipped_count": 0,
        "error_count": 0,
        "seed": base_seed,
        "stats": _empty_stats(),
    }

    out_buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, \
         zipfile.ZipFile(out_buf, "w", compression=zipfile.ZIP_DEFLATED) as zout:

        infos = zin.infolist()
        for idx, info in enumerate(infos):
            name = info.filename
            # Skip our own report from a previous round-trip, and macOS metadata.
            if name == "_strand-report.txt" or name.startswith("__MACOSX/"):
                continue
            if info.is_dir():
                zout.writestr(info, b"")
                continue

            suffix = _suffix_of(name)

            if suffix not in SUPPORTED_SUFFIXES:
                # Pass through unchanged; record as skipped.
                try:
                    raw = zin.read(info)
                    zout.writestr(info, raw)
                except Exception as exc:
                    report["error_count"] += 1
                    report["entries"].append({
                        "name": name, "out_name": None,
                        "status": "errored", "reason": f"read failed: {exc.__class__.__name__}",
                    })
                    continue
                report["skipped_count"] += 1
                report["entries"].append({
                    "name": name, "out_name": name,
                    "status": "skipped",
                    "reason": f"unsupported type ({suffix or 'no extension'})",
                })
                continue

            # Give each entry its own derived seed so re-running with the same
            # outer seed reproduces every inner result, while inner entries
            # still differ from one another.
            per_entry_seed = (base_seed + idx * 1_000_003) & 0x7FFFFFFF
            entry_opts = InjectOptions(
                rate=opts.rate,
                hairs_per_page=opts.hairs_per_page,
                image_count=opts.image_count,
                palette=opts.palette,
                content_bias=opts.content_bias,
                scale_range=opts.scale_range,
                loop_chance=opts.loop_chance,
                seed=per_entry_seed,
            )

            try:
                raw = zin.read(info)
                processed, entry_stats = strand_bytes(raw, name, entry_opts)
            except Exception as exc:
                report["error_count"] += 1
                report["entries"].append({
                    "name": name, "out_name": None,
                    "status": "errored", "reason": f"{exc.__class__.__name__}: {exc}",
                })
                continue

            _merge_stats(report["stats"], entry_stats)
            out_name = _apply_suffix(name, name_suffix)
            zout.writestr(out_name, processed)
            report["haired_count"] += 1
            report["entries"].append({
                "name": name, "out_name": out_name,
                "status": "hairified", "reason": None,
            })

        # Write the report as the last entry.
        zout.writestr("_strand-report.txt", _format_zip_report(report, opts))

    return out_buf.getvalue(), report


def _format_zip_report(report: dict, opts: InjectOptions) -> str:
    from datetime import datetime, timezone
    lines = [
        "Strand report",
        f"generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"seed:      {report['seed']}",
        f"palette:   {opts.palette}",
        f"rate:      {opts.rate}",
        f"per page:  {opts.hairs_per_page}",
        f"per image: {opts.image_count}",
        "",
        f"hairified: {report['haired_count']}",
        f"skipped:   {report['skipped_count']}",
        f"errored:   {report['error_count']}",
        "",
        "Entries:",
    ]
    for e in report["entries"]:
        if e["status"] == "hairified":
            lines.append(f"  [hairified] {e['name']} -> {e['out_name']}")
        elif e["status"] == "skipped":
            lines.append(f"  [skipped]   {e['name']}  ({e['reason']})")
        else:
            lines.append(f"  [error]     {e['name']}  ({e['reason']})")
    return "\n".join(lines) + "\n"

# Hairify

A tiny novelty filter that drops procedurally-drawn hairs onto images, PDFs,
and PowerPoint files. Drag and drop in, hairified file out.

In the same family as "make this look like a Polaroid" or "convert to ASCII
art" — just for the stray-hair-on-the-photocopier aesthetic.

## What it does

- Generates each hair as a Bezier curve with 4× supersampling, taper, and
  per-pixel colour jitter — so they read as physical strands rather than
  drawn lines.
- Two morphologies: a single curved strand (default) and a laminated
  loop with a tail (occasional, more striking).
- Four palettes: **Dark**, **Mixed**, **Blonde**, **Grey**.
- Six density tiers: **Subtle / Normal / Heavy / Hirsute / Werewolf / Cousin Itt**.
  The last three are the joke — every page gets multiple hairs.
- Content-aware placement: hairs are biased toward detected content
  (text blocks in PDFs, shapes in pptx, edge-dense regions in images)
  rather than splashing on blank margins — about half the time. The
  other half they land somewhere stray, which is what a real hair would do.
- **ZIP uploads**: drop a zip, get a zip. Each supported entry inside is
  hairified; unsupported entries pass through unchanged. A
  `_hairify-report.txt` summarising the run is added to the output.
- **Seed control**: every response includes an `X-Hairify-Seed` header.
  Re-submit with the same seed to reproduce the same hairs exactly, or
  re-roll for a fresh take without re-uploading.
- **Custom filename suffix**: default is `-haired`. Override it (e.g.
  `.v2`) or leave it empty to keep the original name.

Supported file types: PNG, JPG, JPEG, GIF, BMP, WEBP, PDF, PPTX, ZIP.

## Privacy

- Files are processed entirely in memory and discarded as soon as the
  response is sent.
- Filenames and contents are not logged. Only request envelopes
  (method, path, status, output size).
- No persistence layer. No analytics. Nothing leaves the process.

If you'd rather not trust a service at all, you can run it locally — see below.

## Run it locally

Requires Python 3.11+.

```bash
pip install -e .[dev]
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

## Test

```bash
pip install -e .[dev]
pytest
```

Tests generate their own fixtures (a tiny PNG, a 3-page reportlab PDF, a
3-slide pptx) and round-trip each one through the corresponding injector.

## Deploy

### Fly.io

```bash
fly launch --no-deploy           # creates fly.toml interactively, or use the one in repo
fly deploy
```

Edit `fly.toml`'s `app` field to a unique name before the first deploy.

### Docker (anywhere)

```bash
docker build -t hairify .
docker run -p 8000:8000 hairify
```

## API

One endpoint, multipart form. Useful if you want to script this.

```
POST /hairify
  file:        <upload>                                       # required
  palette:     dark|mixed|blonde|grey|brown|white|red         # default: dark
  intensity:   subtle|normal|heavy|hirsute|werewolf|cousin-itt # default: normal
  seed:        <int>                                          # optional — reproduce a prior result
  name_suffix: <string>                                       # optional — default: "-haired"; empty = keep original name
```

Response headers:

- `X-Hairify-Seed` — the seed used (echo it back as `seed=` to reproduce)
- For ZIP responses: `X-Hairify-Haired`, `X-Hairify-Skipped`, `X-Hairify-Errored`

Returns the haired file with `Content-Disposition: attachment`.
Limits: 25 MB max upload, 30 requests / hour / IP, 30 s timeout.

`GET /health` returns `{"ok": true}`.

## Repo layout

```
app/
├─ main.py          # FastAPI app, routes, rate limiting
├─ core.py          # Hair rendering + bytes-based injectors
└─ static/          # Vanilla HTML/JS landing page
tests/
└─ test_inject.py   # Roundtrip tests with generated fixtures
fly.toml
Dockerfile
pyproject.toml
```

## License

MIT.

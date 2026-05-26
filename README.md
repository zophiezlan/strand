# Strand

A tiny novelty filter that drops procedurally-drawn hairs onto images, PDFs,
and PowerPoint files. Drag and drop in, hairified file out — or run the CLI
over a folder.

In the same family as "make this look like a Polaroid" or "convert to ASCII
art" — just for the stray-hair-on-the-photocopier aesthetic.

Two front-ends, one rendering engine:

- **Web service** — drop a file in a browser, get the haired file back. In-memory
  only, nothing persisted.
- **CLI** — point it at a directory, optionally with backups, dry-run, mtime
  preservation, and a manifest you can use to undo a run.

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
  `_strand-report.txt` summarising the run is added to the output.
- **Seed control**: every response includes an `X-Strand-Seed` header.
  Re-submit with the same seed to reproduce the same hairs exactly, or
  re-roll for a fresh take without re-uploading.
- **Custom filename suffix**: default is `-strand`. Override it (e.g.
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

## CLI

`pip install -e .` also installs a `strand` command.

```bash
# Strand every supported file under ./folder, with backups + a manifest.
strand inject ./folder --intensity normal --palette dark --seed 42

# Dry-run first to see what would be touched.
strand inject ./folder --dry-run

# Crank the joke up.
strand inject ./folder --intensity cousin-itt --palette mixed

# Undo a run.
strand restore ./folder/.strand_backups/manifest.json

# Render a grid of sample hairs (good for previewing a palette).
strand preview ./samples.png --palette grey --count 16
```

CLI-specific behaviour, deliberately not in the web version:

- **Directory walking** with skip rules (`.git`, `node_modules`, hidden dirs,
  build artefacts).
- **mtime preservation** (`--preserve-mtime`, on by default; `--no-preserve-mtime`
  to opt out). Filesystem access makes this honest — browsers don't allow it,
  so the web version doesn't pretend to.
- **Backups + manifest**: every modified file is copied to
  `<dir>/.strand_backups/` first; the run's manifest can be passed to
  `strand restore` to roll back.
- **`--dry-run`** to list candidates without changing anything.
- **`--no-backup`** if you're already under version control and don't need
  the safety net.

The CLI uses the same `app/core.py` rendering and placement code as the web
service, so palette / intensity / content-aware placement / seed semantics are
identical across both surfaces.

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
docker build -t strand .
docker run -p 8000:8000 strand
```

## API

One endpoint, multipart form. Useful if you want to script this.

```
POST /strand
  file:        <upload>                                       # required
  palette:     dark|mixed|blonde|grey|brown|white|red         # default: dark
  intensity:   subtle|normal|heavy|hirsute|werewolf|cousin-itt # default: normal
  seed:        <int>                                          # optional — reproduce a prior result
  name_suffix: <string>                                       # optional — default: "-strand"; empty = keep original name
```

Response headers:

- `X-Strand-Seed` — the seed used (echo it back as `seed=` to reproduce)
- For ZIP responses: `X-Strand-Haired`, `X-Strand-Skipped`, `X-Strand-Errored`

Returns the haired file with `Content-Disposition: attachment`.
Limits: 25 MB max upload, 30 requests / hour / IP, 30 s timeout.

`GET /health` returns `{"ok": true}`.

## Repo layout

```
app/
├─ core.py          # Hair rendering + bytes-based injectors (shared)
├─ main.py          # FastAPI app, routes, rate limiting
├─ cli.py           # `strand` CLI — directory walking, backups, mtime
└─ static/          # Vanilla HTML/JS landing page
tests/
├─ test_inject.py   # Core + HTTP roundtrips
└─ test_cli.py      # CLI subcommands end-to-end
fly.toml
Dockerfile
pyproject.toml
```

`app/core.py` is the single source of truth for hair generation, palettes,
content-aware placement, and density tiers. The web app and the CLI are thin
wrappers — keep new rendering behaviour in `core.py` and both surfaces inherit it.

## License

MIT.

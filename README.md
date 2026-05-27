# Strand

A tiny novelty filter that drops procedurally-drawn hairs onto images, PDFs,
and PowerPoint files. Drag and drop in, hairified file out — or run the CLI
over a folder.

In the same family as "make this look like a Polaroid" or "convert to ASCII
art" — just for the stray-hair-on-the-photocopier aesthetic.

Two front-ends, one rendering engine:

- **Web page** — drop a file in your browser, get the haired file back. By
  default the whole pipeline runs in-browser via Pyodide — your file
  never leaves the page. A "process on server" toggle is there for
  browsers that struggle with very large files.
- **CLI** — point it at a directory, optionally with backups, dry-run, mtime
  preservation, and a manifest you can use to undo a run.

## What it does

- Generates each hair as a Bezier curve with 4× supersampling, taper, and
  per-pixel colour jitter — so they read as physical strands rather than
  drawn lines.
- Five morphologies mixed by default: curved strand, laminated loop with
  tail, eyelash (short tight curl), fragment (short wispy piece), and the
  occasional kink (tight zigzag, body-hair vibe).
- ~18% of shed strands carry a tiny dark follicle bulb at one end — the
  "club" root that fell out with the hair. Single most diagnostic "yep,
  that's a real hair" detail.
- Seven palettes: **Dark / Brown / Blonde / Red / Grey / White / Mixed**.
- Six density tiers: **Subtle / Normal / Heavy / Hirsute / Werewolf / Cousin Itt**.
  The last three are the joke — every page gets multiple hairs.
- Defaults are tuned to mimic the classic photocopier-glass / laminator-pocket
  artefact: a single light hair. That's the most convincing "real stray
  hair" look; the heavier tiers and bolder palettes are for play.
- Hairs occasionally clump into small tufts (configurable `cluster_chance`) —
  real shed hair doesn't space itself evenly.
- Content-aware placement: hairs are biased toward detected content
  (text blocks in PDFs, shapes in pptx, edge-dense regions in images)
  rather than splashing on blank margins — about half the time. The
  other half they land somewhere stray, which is what a real hair would do.
- **Multi-file & folder uploads**: drop several files (or a whole folder) and
  Strand bundles them into a zip on its way out, preserving structure.
- **ZIP uploads**: drop a zip, get a zip. Each supported entry inside is
  hairified; unsupported entries pass through unchanged. A
  `_strand-report.txt` summarising the run is added to the output.
- **Paste from clipboard**: paste a screenshot directly into the page —
  it goes straight into the upload slot.
- **Seed control**: every response includes an `X-Strand-Seed` header.
  Re-submit with the same seed to reproduce the same hairs exactly, or
  re-roll for a fresh take without re-uploading.
- **Custom filename suffix**: default is `-strand`. Override it (e.g.
  `.v2`) or leave it empty to keep the original name.

Supported file types: PNG, JPG, JPEG, GIF, BMP, WEBP, PDF, PPTX, ZIP.

## Privacy

The web page runs Strand entirely in your browser by default. On first
upload it lazy-loads Pyodide and runs the same `app/core.py` rendering engine
the CLI uses, locally. Your file is read, processed, and previewed inside the
page — nothing is uploaded.

The engine only downloads what the dropped file needs: the runtime plus Pillow
(~13 MB) for images, adding PyMuPDF (~17 MB) only when you strand a PDF, and
python-pptx only for `.pptx`. In production these are served from the app's own
origin (vendored at build time by `scripts/fetch_pyodide.py`, cached
immutably); in plain local dev the worker falls back to the jsdelivr CDN. Once
fetched, the browser caches them, so subsequent uploads start instantly.

The "process on server instead" toggle is the explicit opt-in. When ticked,
the file is POSTed to the same FastAPI service the project has always
shipped:

- Processed entirely in memory, discarded as soon as the response is sent.
- Filenames and contents are not logged. Only request envelopes
  (method, path, status, output size).
- No persistence layer. No analytics.

Same applies to the `POST /strand` API — it's the server path. If you call
it from a script, you're sending your file to the server.

## Run it locally

Requires Python 3.11+.

```bash
pip install -e .[dev]
uvicorn app.main:app --reload
# open http://127.0.0.1:8000
```

The server hosts the static page (which boots Pyodide on first drop) and
also handles the `/strand` POST route used by the server-mode toggle and
the public API.

In local dev the in-browser engine boots from the jsdelivr CDN. To exercise
the production path (engine served from this origin), vendor the runtime first:

```bash
python scripts/fetch_pyodide.py   # writes app/static/pyodide/ (~32 MB, gitignored)
```

The Docker build runs this automatically; the directory is never committed.

## CLI

`pip install -e .` also installs a `strand` command.

```bash
# Strand every supported file under ./folder, with backups + a manifest.
strand inject ./folder --intensity normal --palette dark --seed 42

# A single file works too. Default is overwrite-in-place + backup.
strand inject ./photo.png --seed 42

# Sibling write: leave photo.png alone, write photo-strand.png next to it.
# Use `=` because the value starts with a dash.
strand inject ./photo.png --name-suffix=-strand

# Non-destructive bulk run: mirror the source tree under ./out/, originals untouched.
strand inject ./folder --output-dir ./out

# Zips are handled too — unpacked, each supported entry hairified, repacked
# with a _strand-report.txt inside. Same flag-set as everything else.
strand inject ./pack.zip --name-suffix=-strand

# Filter by glob (repeatable). Matched against both the relative path and the basename.
strand inject ./folder --include '*.pdf' --exclude 'drafts/**'

# Dry-run first to see what would be touched.
strand inject ./folder --dry-run

# Crank the joke up.
strand inject ./folder --intensity cousin-itt --palette mixed

# Machine-readable summary for scripting.
strand inject ./folder --json --no-backup

# Undo a run.
strand restore ./folder/.strand_backups/manifest.json

# Or just: find the most recent manifest under cwd and restore from it.
strand undo

# Quick discoverability — palettes, intensities, morphologies, supported types.
strand list

# Render a grid of sample hairs (good for previewing a palette).
strand preview ./samples.png --palette grey --count 16

# Or one hair on its own. Pipe to stdout for shell-y workflows.
strand sample one.png --palette mixed --morphology eyelash --seed 42
strand sample - --palette dark > a-hair.png
```

CLI-specific behaviour, deliberately not in the web version:

- **File, directory, or zip** as a target. The zip path mirrors the web —
  contents are hairified in place and `_strand-report.txt` lands inside.
- **Directory walking** with skip rules (`.git`, `node_modules`, hidden dirs,
  build artefacts).
- **`--include` / `--exclude` glob filters** (repeatable) for restricting
  what gets touched on a directory walk.
- **`--name-suffix`** to write a renamed copy alongside the original (e.g.
  `photo-strand.png`) instead of overwriting in place. Same semantics as the
  web's filename-suffix.
- **`--output-dir`** to write outputs into a separate directory tree,
  mirroring the source layout. Originals stay untouched.
- **mtime preservation** (`--preserve-mtime`, on by default; `--no-preserve-mtime`
  to opt out). Only applies to in-place overwrites. Filesystem access makes
  this honest — browsers don't allow it, so the web version doesn't pretend to.
- **Backups + manifest**: when overwriting in place, every modified file is
  copied to `<target>/.strand_backups/` first; the run's manifest can be
  passed to `strand restore` to roll back. Automatically skipped when
  `--name-suffix` or `--output-dir` is set (originals already preserved).
- **`--dry-run`** to list candidates without changing anything.
- **`--no-backup`** if you're already under version control and don't need
  the safety net.
- **`--json`** emits a structured report (per-file stats, aggregate hair
  counts, content-aware ratios) instead of human-readable prose. Output is
  pure ASCII — no progress bar, no ANSI colour — safe to pipe into `jq`.
- **`--quiet` / `--verbose`** dial the noise level. Verbose adds per-file
  stats; quiet suppresses everything but failures.
- **Progress bar + colourised output** in interactive terminals via
  [Rich](https://github.com/Textualize/rich). Auto-disabled when stdout is
  piped or redirected, so `strand inject ... | grep haired` still works.
- **`strand undo`** finds the most-recent `.strand_backups/manifest.json`
  under cwd (or a given path) and restores from it — no need to type out
  the manifest path.
- **Confirmation prompt** before in-place runs with `--no-backup` on more
  than a handful of files. Pass `--yes` / `-y` to skip, or run from a
  non-interactive shell (CI etc.) where the prompt is bypassed automatically.

### Shell completion

The CLI registers tab-completion via
[`argcomplete`](https://kislyuk.github.io/argcomplete/). Activate it once per
shell:

```bash
# bash / zsh (one-time global install — no per-script wiring needed)
activate-global-python-argcomplete --user

# fish
register-python-argcomplete --shell fish strand | source

# Or, per-shell, just for `strand`:
eval "$(register-python-argcomplete strand)"
```

Then `strand inject --pal<TAB>` will complete the flag and offer the seven
palette names; `--intensity <TAB>` lists the six density tiers; positional
arguments tab-complete to filenames.

### Version

```bash
strand --version
```

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
  palette:     dark|mixed|blonde|grey|brown|white|red         # default: white
  intensity:   subtle|normal|heavy|hirsute|werewolf|cousin-itt # default: subtle
  seed:        <int>                                          # optional — reproduce a prior result
  name_suffix: <string>                                       # optional — default: "-strand"; empty = keep original name
```

You can also send multiple `file` fields in the same request (or one zip).
Multiple uploads always come back as a single zip; sub-paths are preserved.

Response headers:

- `X-Strand-Seed` — the seed used (echo it back as `seed=` to reproduce)
- For ZIP responses: `X-Strand-Haired`, `X-Strand-Skipped`, `X-Strand-Errored`
- For 500s: `X-Strand-Error-Id` (also embedded in the JSON `detail`)

`GET /api/sample?palette=<name>&seed=<int>&morphology=<name>` returns one
hair on transparent PNG (cached an hour). Used internally by the chip
previews on the landing page, but also handy for embedding samples
elsewhere.

Returns the haired file with `Content-Disposition: attachment`.
Limits: 25 MB max upload, 30 requests / hour / IP, 30 s timeout.

`GET /health` returns `{"ok": true}`.

## Repo layout

```
app/
├─ core.py          # Hair rendering + bytes-based injectors (shared)
├─ main.py          # FastAPI app, routes, rate limiting, serves /core.py to the browser
├─ cli.py           # `strand` CLI — directory walking, backups, mtime
└─ static/
   ├─ index.html
   ├─ app.js               # UI, dispatches to engine or server based on toggle
   ├─ pyodide_engine.js    # main-thread RPC shim over the worker
   ├─ pyodide_worker.js    # boots Pyodide in a Worker, lazy-loads libs per file type
   ├─ pyodide/             # vendored runtime + wheels (build-time, gitignored)
   └─ style.css
scripts/
└─ fetch_pyodide.py # vendors the Pyodide runtime + wheels into app/static/pyodide/
tests/
├─ test_inject.py   # Core + HTTP roundtrips
└─ test_cli.py      # CLI subcommands end-to-end
fly.toml
Dockerfile
pyproject.toml
```

`app/core.py` is the single source of truth for hair generation, palettes,
content-aware placement, and density tiers. The CLI imports it directly;
the FastAPI server imports it and also serves it at `/core.py` so the
browser-side Pyodide engine runs the exact same code. Keep new rendering
behaviour in `core.py` and all three surfaces inherit it.

## License

MIT.

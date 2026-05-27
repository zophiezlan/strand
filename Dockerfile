FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# PyMuPDF wheels are self-contained on Linux x86_64; no extra system libs needed
# for our usage. python-pptx and Pillow are pure-Python / wheels.

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app
COPY scripts ./scripts

# Vendor the Pyodide runtime + wheels into app/static/pyodide/ so the in-browser
# engine boots from this origin instead of a third-party CDN. Stdlib-only, needs
# network at build. If this layer is ever skipped the worker falls back to the CDN.
RUN python scripts/fetch_pyodide.py

RUN pip install .

# Fly.io and Render both inject $PORT; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

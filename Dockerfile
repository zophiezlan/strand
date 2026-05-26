FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# PyMuPDF wheels are self-contained on Linux x86_64; no extra system libs needed
# for our usage. python-pptx and Pillow are pure-Python / wheels.

WORKDIR /app

COPY pyproject.toml ./
COPY app ./app

RUN pip install .

# Fly.io and Render both inject $PORT; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]

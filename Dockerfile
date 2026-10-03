# syntax=docker/dockerfile:1
# ─────────────────────────────────────────────────────────────────────────────
# stage 1: builder — install dependencies into a separate venv, keeping the toolchain out of the runtime image
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Copy requirements.txt before the source, so this layer stays cached until a dependency changes
COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# ─────────────────────────────────────────────────────────────────────────────
# stage 2: runtime
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# Verified that the wheels in use need no additional OS libraries:
#   opencv-python-headless needs no libgl1 (it has no GUI backend), and 5.x bundles libglib itself
#   pillow-heif bundles libheif, and psycopg[binary] bundles libpq, both inside the wheel
# So only curl is installed, for the HEALTHCHECK to use.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8000

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# Only what runtime needs: the code, templates, static files, and schema.sql (used by db init)
COPY ocrslip ./ocrslip
COPY db ./db

# Run as a non-root user
RUN useradd --create-home --uid 10001 ocrslip && chown -R ocrslip:ocrslip /app
USER ocrslip

EXPOSE 8000

# /static/app.css answers 200 without a DB connection and without a login, so it checks whether
# the process is still serving requests
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/static/app.css" > /dev/null || exit 1

# Render supplies $PORT at runtime, so this has to be the shell form rather than the exec form
CMD ["sh", "-c", "exec uvicorn ocrslip.web.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]

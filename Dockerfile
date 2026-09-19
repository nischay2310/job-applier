# syntax=docker/dockerfile:1.7

# ---------- Stage 1: frontend build (cached until frontend/ changes) ----------
FROM node:22.12-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---------- Stage 2: browser binaries — PINNED & fully isolated. ----------
# This stage does NOT depend on pyproject.toml, uv.lock, or src/ in any way,
# so editing app code or Python deps can never invalidate it. It only
# rebuilds when PLAYWRIGHT_VERSION below is bumped on purpose.
# Keep this in sync with the playwright version pinned in pyproject.toml/uv.lock.
FROM python:3.12-slim-bookworm AS browsers
ARG PLAYWRIGHT_VERSION=1.62.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir --retries 5 --timeout 120 "playwright==${PLAYWRIGHT_VERSION}" \
    && playwright install --with-deps chromium

# App user + runtime dirs live here too — they don't depend on app code
# or Python deps either, so they're baked into this same cached, pinned layer.
RUN groupadd --gid 10001 job-applier \
    && useradd --uid 10001 --gid job-applier --home-dir /app --no-create-home --shell /usr/sbin/nologin job-applier \
    && mkdir -p /app/data /app/output/applications /app/output/applied /app/.browser_profile \
    && chown -R job-applier:job-applier /app

# ---------- Stage 3: python project deps (cached until pyproject.toml/uv.lock change) ----------
FROM python:3.12-slim-bookworm AS deps
ARG UV_VERSION=0.8.9
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---------- Stage 4: runtime ----------
# Chains from `browsers` (Chromium + system libs — cached, untouched by code
# or dependency churn). The deps venv and app code are copied in as the last,
# cheap steps so day-to-day rebuilds only redo this stage.
FROM browsers AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app
COPY --from=deps /usr/local/bin/uv /usr/local/bin/uv
COPY --from=deps /usr/local/bin/uvx /usr/local/bin/uvx
COPY --from=deps /app/.venv /app/.venv
COPY pyproject.toml uv.lock ./
COPY src/ ./src/
COPY frontend/public/ ./frontend/public/
COPY --from=frontend-builder /build/frontend/dist/ ./frontend/dist/
COPY deploy/defaults/ /opt/job-applier-defaults/
COPY deploy/docker-entrypoint.sh /usr/local/bin/job-applier-entrypoint

RUN uv sync --frozen --no-dev \
    && uv pip install watchdog[watchmedo] \
    && chmod 0755 /usr/local/bin/job-applier-entrypoint

USER job-applier
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"]

ENTRYPOINT ["job-applier-entrypoint"]
CMD ["/app/.venv/bin/python", "-m", "job_applier.cli.web_app", "--host", "0.0.0.0", "--port", "8000", "--no-browser", "--no-reload"]

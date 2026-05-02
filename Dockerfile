# syntax=docker/dockerfile:1
# polymarket_hybrid_bot — multi-stage runtime (v0.2.1)

FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        libffi-dev \
        libssl-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --shell /sbin/nologin --create-home bot

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=bot:bot . /app

USER bot

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD test -f /tmp/.bot_alive \
    && test "$(($(date +%s) - $(stat -c %Y /tmp/.bot_alive 2>/dev/null || echo 0)))" -lt 90 \
    || exit 1

CMD ["python", "-m", "polymarket_hybrid_bot.main"]

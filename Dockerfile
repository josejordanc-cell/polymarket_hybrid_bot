# syntax=docker/dockerfile:1
# polymarket_hybrid_bot — multi-stage runtime (v0.2.4)

# ---- Builder ---------------------------------------------------------------
# Pin by digest to make the build reproducible and prevent supply-chain
# tampering via `latest`-style tag mutation. Bump intentionally on each
# security advisory rebuild — see DEPLOY_DO.md "Update and patching cadence".
FROM python:3.11-slim-bookworm@sha256:ee710afcfb733f4a750d9be683cf054b5cd247b6c5f5237a6849ea568b90ab15 AS builder

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
# When requirements.txt is migrated to hash-pinned form (pip-compile
# --generate-hashes), add `--require-hashes` here. Tracked in HANDOFF.md.
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt

# ---- Runtime ---------------------------------------------------------------
FROM python:3.11-slim-bookworm@sha256:ee710afcfb733f4a750d9be683cf054b5cd247b6c5f5237a6849ea568b90ab15 AS runtime

LABEL org.opencontainers.image.title="polymarket_hybrid_bot" \
      org.opencontainers.image.source="https://github.com/josejordanc-cell/polymarket_hybrid_bot" \
      org.opencontainers.image.licenses="UNLICENSED" \
      org.opencontainers.image.version="0.2.4"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --shell /usr/sbin/nologin --create-home bot

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=bot:bot . /app

USER bot

# tini is PID 1 — it reaps zombies and forwards SIGTERM to the bot,
# allowing the asyncio shutdown handler to cancel open orders cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD test -f /tmp/.bot_alive \
    && test "$(($(date +%s) - $(stat -c %Y /tmp/.bot_alive 2>/dev/null || echo 0)))" -lt 90 \
    || exit 1

CMD ["python", "-m", "polymarket_hybrid_bot.main"]

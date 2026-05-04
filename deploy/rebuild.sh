#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"

if [[ ! -f "$ENV_FILE" ]]; then
    cat >&2 <<'EOF'
ERROR: .env is missing.

Copy the example file, fill in all [SECRET] fields, then lock down permissions:

  cp .env.example .env
  $EDITOR .env
  chmod 600 .env
EOF
    exit 1
fi

env_mode=""
if env_mode=$(stat -c '%a' "$ENV_FILE" 2>/dev/null); then
    :
elif env_mode=$(stat -f '%A' "$ENV_FILE" 2>/dev/null); then
    :
else
    echo "ERROR: could not read .env file mode (stat failed)." >&2
    exit 1
fi

if [[ "$env_mode" != "600" ]]; then
    echo "ERROR: .env must have mode 600 (found ${env_mode}). World-readable .env files leak credentials." >&2
    echo "Fix with:  chmod 600 .env" >&2
    exit 1
fi

cd "$ROOT"

NO_TAIL=false
NUKE=false

usage() {
    sed -n '1,80p' <<'EOF'
Usage: ./deploy/rebuild.sh [--no-tail] [--nuke] [-h|--help]

  Rebuild and restart the Docker Compose stack from the repository root.
  --no-tail   Do not follow logs after a successful readiness check
  --nuke      Prune dangling images and build cache before build
  -h, --help  Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-tail) NO_TAIL=true ;;
        --nuke) NUKE=true ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
    esac
    shift
done

if ! command -v docker >/dev/null 2>&1; then
    echo "docker not found in PATH" >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "docker compose (v2) not available" >&2
    exit 1
fi

docker compose down --remove-orphans

if [[ "$NUKE" == true ]]; then
    docker image prune -f
    docker builder prune -f
fi

if ! docker compose build --pull; then
    echo "ERROR: docker compose build failed" >&2
    exit 1
fi

docker compose up -d

deadline=$((SECONDS + 60))
found=""
while [[ $SECONDS -lt $deadline ]]; do
    if docker compose logs --since=2m bot 2>/dev/null | grep -q "strategy_loop_started"; then
        found=1
        break
    fi
    sleep 2
done

if [[ -n "$found" ]]; then
    echo "OK: strategy_loop_started observed in bot logs."
    if [[ "$NO_TAIL" == true ]]; then
        exit 0
    fi
    exec docker compose logs -f --tail=20 bot
fi

echo "ERROR: strategy_loop_started not found within 60s" >&2
docker compose logs --tail=50 bot >&2 || true
exit 2

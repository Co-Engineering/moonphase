#!/usr/bin/env bash
#
# Bring up the whole Moonphase development stack: Supabase, the API, Vite, and
# the Electron shell. Ctrl-C stops everything it started.
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

blue() { printf '\033[34m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }

for tool in docker pnpm uv supabase; do
  command -v "$tool" >/dev/null 2>&1 || { red "missing required tool: $tool"; exit 1; }
done

if [[ ! -f .env ]]; then
  red "No .env found. Run: cp .env.example .env"
  red "Then set MOONPHASE_SECRET_KEY:"
  red '  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

API_PORT="${MOONPHASE_API_PORT:-8471}"

blue "==> Supabase"
if ! supabase status >/dev/null 2>&1; then
  supabase start
else
  echo "    already running"
fi
supabase db push --local >/dev/null 2>&1 || supabase migration up --local || true

blue "==> Runtime image"
if ! docker image inspect "${MOONPHASE_RUNTIME_IMAGE:-moonphase/runtime-claude:latest}" >/dev/null 2>&1; then
  docker build -t "${MOONPHASE_RUNTIME_IMAGE:-moonphase/runtime-claude:latest}" infra/images/claude/
else
  echo "    ${MOONPHASE_RUNTIME_IMAGE:-moonphase/runtime-claude:latest} present"
fi

blue "==> API dependencies"
cd apps/api
[[ -d .venv ]] || uv venv --python 3.11
uv pip install -q -e ".[dev]"
cd "$ROOT"

blue "==> Frontend dependencies"
pnpm install --silent

pids=()
cleanup() {
  echo
  blue "==> stopping"
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

blue "==> API on :${API_PORT}"
(cd apps/api && .venv/bin/python -m uvicorn moonphase.main:app \
  --host 127.0.0.1 --port "$API_PORT" --reload) &
pids+=($!)

blue "==> Vite on :8472"
pnpm --filter @moonphase/web dev &
pids+=($!)

# Wait for Vite before Electron, or the window opens on a connection error.
for _ in $(seq 1 60); do
  curl -sf http://127.0.0.1:8472 >/dev/null 2>&1 && break
  sleep 0.5
done

blue "==> Electron"
pnpm --filter @moonphase/desktop dev &
pids+=($!)

blue "Moonphase is up. Ctrl-C to stop."
wait

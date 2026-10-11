#!/usr/bin/env bash
# End-to-end test: Open WebUI -> (mock LLM) tool call -> authenticated tool server -> image.
#
#   ./e2e/run.sh            # or: make e2e
#
# Runs an isolated stack (own compose project, own ports, throw-away secrets) so it never
# touches your normal .env or volumes. Needs Docker, openssl and python3.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT=${E2E_PROJECT:-owui-image-tool-e2e}
TOOL_PORT=${E2E_TOOL_PORT:-18001}
WEBUI_PORT=${E2E_WEBUI_PORT:-13000}
MOCK_LLM_PORT=${E2E_MOCK_LLM_PORT:-19001}

umask 077
env_file=$(mktemp)
rand() { openssl rand -hex 24; }
tool_key=$(rand)
admin_password=$(rand)
cat >"$env_file" <<EOF
TOOL_API_KEY=$tool_key
URL_SIGNING_KEY=$(rand)
WEBUI_SECRET_KEY=$(rand)
WEBUI_ADMIN_EMAIL=e2e@example.com
WEBUI_ADMIN_PASSWORD=$admin_password
TOOL_PORT=$TOOL_PORT
WEBUI_PORT=$WEBUI_PORT
MOCK_LLM_PORT=$MOCK_LLM_PORT
IMAGE_PROVIDER=openai
IMAGE_API_KEY=mock-image-key
IMAGE_API_BASE_URL=http://mock-llm:8080/v1
IMAGE_API_ALLOW_INSECURE=true
EOF

compose=(docker compose -p "$PROJECT" --env-file "$env_file"
         -f docker-compose.yml -f e2e/docker-compose.e2e.yml)

cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then
    echo "--- e2e failed; recent container logs ---" >&2
    "${compose[@]}" logs --tail=40 open-webui image-tool mock-llm >&2 || true
  fi
  "${compose[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$env_file"
  exit $status
}
trap cleanup EXIT

echo "==> starting isolated stack ($PROJECT)"
"${compose[@]}" up -d --build --wait

echo "==> running assertions"
E2E_WEBUI_URL="http://127.0.0.1:$WEBUI_PORT" \
E2E_TOOL_URL="http://127.0.0.1:$TOOL_PORT" \
E2E_MOCK_URL="http://127.0.0.1:$MOCK_LLM_PORT" \
E2E_EMAIL="e2e@example.com" \
E2E_PASSWORD="$admin_password" \
  python3 e2e/run_e2e.py

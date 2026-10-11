#!/usr/bin/env bash
# Black-box checks against a running tool server (no Open WebUI involved).
#
#   ./scripts/smoke-test.sh                       # uses TOOL_API_KEY / TOOL_PORT from .env
#   BASE_URL=http://127.0.0.1:8001 TOOL_API_KEY=... ./scripts/smoke-test.sh
set -euo pipefail
cd "$(dirname "$0")/.."

env_value() { [[ -f .env ]] && grep -E "^$1=" .env | head -n1 | cut -d= -f2- || true; }

TOOL_API_KEY="${TOOL_API_KEY:-$(env_value TOOL_API_KEY)}"
TOOL_PORT="${TOOL_PORT:-$(env_value TOOL_PORT)}"
BASE_URL="${BASE_URL:-http://127.0.0.1:${TOOL_PORT:-8001}}"
[[ -n "$TOOL_API_KEY" ]] || { echo "TOOL_API_KEY is not set (run ./scripts/init-env.sh)." >&2; exit 2; }
command -v jq >/dev/null || { echo "jq is required." >&2; exit 2; }

pass=0
check() { # description, expected, actual
  if [[ "$2" == "$3" ]]; then printf '  ok    %s\n' "$1"; pass=$((pass + 1))
  else printf '  FAIL  %s (expected %s, got %s)\n' "$1" "$2" "$3" >&2; exit 1; fi
}
status() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

echo "Smoke test against $BASE_URL"
check "healthz is public"                200 "$(status "$BASE_URL/healthz")"
check "no API key is rejected"           401 "$(status -X POST "$BASE_URL/generate_image" \
  -H 'Content-Type: application/json' -d '{"prompt":"a red bicycle"}')"
check "wrong API key is rejected"        401 "$(status -X POST "$BASE_URL/generate_image" \
  -H 'Authorization: Bearer definitely-not-the-key' -H 'Content-Type: application/json' \
  -d '{"prompt":"a red bicycle"}')"

operations=$(curl -fsS "$BASE_URL/openapi.json" | jq -r '[.paths[][].operationId] | join(",")')
check "OpenAPI exposes generate_image"   generate_image "$operations"

response=$(curl -fsS -X POST "$BASE_URL/generate_image" \
  -H "Authorization: Bearer $TOOL_API_KEY" -H 'Content-Type: application/json' \
  -d '{"prompt":"A watercolor lighthouse at dawn, soft pastel sky","aspect":"landscape"}')
image_url=$(jq -r '.image_url' <<<"$response")
check "generate_image returns markdown"  true "$(jq -r '.markdown | startswith("![")' <<<"$response")"
check "image URL is signed"              true "$(jq -r '.image_url | test("[?]exp=[0-9]+&sig=")' <<<"$response")"

# The browser-facing URL may use a different host than we do here; fetch via BASE_URL.
path_and_query="/images/${image_url##*/images/}"
check "signed image URL downloads"       200 "$(status "$BASE_URL$path_and_query")"
content_type=$(curl -fsS -o /dev/null -w '%{content_type}' "$BASE_URL$path_and_query")
check "image has an image/* type"        image "${content_type%%/*}"
check "tampered signature is rejected"   403 "$(status "$BASE_URL${path_and_query%?}x")"

echo "All $pass checks passed."

#!/usr/bin/env bash
# Create .env from .env.example with freshly generated secrets.
#
#   ./scripts/init-env.sh
#
# Optional: export OPENAI_API_KEY first, or type it at the (hidden) prompt. The key is
# written only to .env (mode 600); it is never echoed or placed on a command line.
set -euo pipefail
umask 077

cd "$(dirname "$0")/.."

if [[ -e .env ]]; then
  echo ".env already exists - refusing to overwrite it. Delete it first to regenerate." >&2
  exit 1
fi
command -v openssl >/dev/null || { echo "openssl is required to generate secrets." >&2; exit 1; }

# Work on a temp file in the project directory (same filesystem => atomic rename; matched
# by the .env.* git-ignore rule) and make sure it never outlives the script.
tmp=""
trap '[[ -z "$tmp" ]] || rm -f "$tmp"' EXIT

cp .env.example .env
chmod 600 .env

# Set NAME=value in .env (appending it if the template lacks the key). The value goes
# through the environment, so awk never interprets it.
set_var() {
  local name=$1 value=$2
  tmp=$(mktemp .env.XXXXXX)
  VALUE="$value" awk -v name="$name" 'BEGIN { FS = "=" }
    $1 == name { print name "=" ENVIRON["VALUE"]; seen = 1; next }
    { print }
    END { if (!seen) print name "=" ENVIRON["VALUE"] }' .env >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" .env
  tmp=""
}

admin_password=$(openssl rand -base64 24 | tr -d '/+=\n' | cut -c1-24)

set_var TOOL_API_KEY "$(openssl rand -hex 32)"
set_var URL_SIGNING_KEY "$(openssl rand -hex 32)"
set_var WEBUI_SECRET_KEY "$(openssl rand -hex 32)"
set_var WEBUI_ADMIN_EMAIL "admin@example.com"
set_var WEBUI_ADMIN_PASSWORD "$admin_password"

openai_key="${OPENAI_API_KEY:-}"
if [[ -z "$openai_key" && -t 0 ]]; then
  read -rsp "OpenAI API key for chat + image generation (optional, Enter to skip): " openai_key
  echo
fi
if [[ -n "$openai_key" ]]; then
  # The same key is used for the chat model and for images. They are separate settings
  # on purpose: the image key is only ever sent to IMAGE_API_BASE_URL.
  set_var OPENAI_API_KEY "$openai_key"
  set_var IMAGE_API_KEY "$openai_key"
fi

cat <<EOF

Created .env (mode 600) with generated secrets.

  Open WebUI : http://localhost:3000
  Admin login: admin@example.com
  Password   : $admin_password      (also stored in .env)

Next:  docker compose up -d --build --wait
EOF
if [[ -z "$openai_key" ]]; then
  echo
  echo "No API key set: images come from the offline mock provider, and Open WebUI needs"
  echo "a chat model - set OPENAI_API_KEY (and IMAGE_API_KEY for real images) in .env."
fi

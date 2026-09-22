#!/bin/sh
# Start the Hatch web remote. Pinned to the BLE Rest (not the wifi unit).
# Bound to 127.0.0.1 only; reach it from outside via a tunnel/reverse proxy of
# your choice. The app itself requires HATCH_PASSWORD.
cd "$(dirname "$0")"

# Secrets live in .env (gitignored). See .env.example.
[ -f .env ] && . ./.env

export HATCH_ADDR  # from scan.py; set in .env
export HATCH_PASSWORD
if [ -z "$HATCH_PASSWORD" ]; then
  echo "HATCH_PASSWORD not set -- copy .env.example to .env and set it" >&2
  exit 1
fi

# stable signing secret so logins survive restarts
[ -f .secret ] || .venv/bin/python -c 'import secrets;print(secrets.token_hex(32))' > .secret
export HATCH_SECRET="$(cat .secret)"

exec .venv/bin/uvicorn server:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}" --proxy-headers --forwarded-allow-ips='*'

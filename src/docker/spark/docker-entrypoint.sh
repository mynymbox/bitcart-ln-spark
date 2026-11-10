#!/bin/sh
set -e

AUTH_FILE="${SPARK_BRIDGE_AUTH_FILE:-/auth/secret}"
mkdir -p "$(dirname "$AUTH_FILE")"

if [ -z "${SPARK_BRIDGE_SECRET:-}" ]; then
  if [ ! -s "$AUTH_FILE" ]; then
    SPARK_BRIDGE_AUTH_FILE="$AUTH_FILE" node --input-type=module -e "
      import { randomBytes } from 'node:crypto';
      import { writeFileSync } from 'node:fs';
      writeFileSync(process.env.SPARK_BRIDGE_AUTH_FILE, randomBytes(32).toString('hex'), { mode: 0o644 });
    "
  fi
  chmod 644 "$AUTH_FILE" 2>/dev/null || true
fi

if [ "$(id -u)" = "0" ] && command -v runuser >/dev/null 2>&1; then
  exec runuser -u node -- node server.mjs
fi

exec node server.mjs

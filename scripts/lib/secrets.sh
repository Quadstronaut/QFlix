#!/usr/bin/env bash
# Read/write helpers for secrets/<name>.<ext> files. Trims whitespace.
SECRETS_DIR="${SECRETS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../secrets" && pwd)}"

secret_read()  { local f="$SECRETS_DIR/$1"; [ -f "$f" ] || die "missing secret: $f"; tr -d '[:space:]' < "$f"; }
secret_write() { local f="$SECRETS_DIR/$1"; mkdir -p "$(dirname "$f")"; printf '%s\n' "$2" > "$f"; chmod 600 "$f"; }
secret_exists(){ [ -f "$SECRETS_DIR/$1" ]; }

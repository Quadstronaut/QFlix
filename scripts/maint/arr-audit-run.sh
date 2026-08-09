#!/usr/bin/env bash
# arr-audit-run — wrapper for the weekly manitoba-maint-arr-audit.service.
#
# Invokes ~/scripts/maint/arr-audit.py in loopback mode, captures the
# markdown output to ~/.opt/maint/audit-reports/arr-audit-YYYY-MM-DD.md,
# and prunes reports older than 90 days so the directory doesn't grow
# unbounded.
#
# Reads QFLIX_ARR_AUDIT_LOOPBACK from the environment (the service unit
# sets it to 1). If the env var is unset we still proceed — arr-audit.py
# will fall back to public-URL mode via secrets/seedbox.host.
set -uo pipefail

REPORT_DIR="${MANITOBA_STATE_DIR:-$HOME/.opt/maint}/audit-reports"
SCRIPT="$HOME/scripts/maint/arr-audit.py"
DATE="$(date -u +%Y-%m-%d)"
OUT="$REPORT_DIR/arr-audit-$DATE.md"

mkdir -p "$REPORT_DIR"

if [ ! -f "$SCRIPT" ]; then
    echo "FATAL: $SCRIPT not found — was 240-maintenance-install.sh updated?" >&2
    exit 2
fi

if ! python3 "$SCRIPT" > "$OUT" 2> "$OUT.err"; then
    echo "arr-audit exited non-zero — see $OUT and $OUT.err" >&2
    # Surface stderr in journal but keep the partial report file.
    cat "$OUT.err" >&2
    exit 1
fi

# Stderr from arr-audit is per-call HTTP errors — keep it alongside the
# report if it's non-empty, otherwise discard.
if [ ! -s "$OUT.err" ]; then
    rm -f "$OUT.err"
fi

# Prune anything older than 90 days. Use -mtime not -atime so the audit
# history reflects when each report was generated, not last opened.
find "$REPORT_DIR" -maxdepth 1 -name 'arr-audit-*.md*' -type f -mtime +90 -delete 2>/dev/null || true

echo "arr-audit ok — report written to $OUT"

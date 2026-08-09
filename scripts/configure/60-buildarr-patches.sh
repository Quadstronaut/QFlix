#!/usr/bin/env bash
# Apply QFlix-maintained patches to the buildarr venv so it can manage
# Sonarr v4+ / Radarr v5+/v6 instances. Companion to 50-buildarr-install.sh.
#
# WHY THIS EXISTS
#   buildarr-sonarr 0.6.4 + buildarr-radarr 0.2.6 (latest PyPI as of 2026-05-11)
#   were written against Sonarr v3 / Radarr v3 schemas. Plugin maintenance has
#   stalled while the upstream *arrs moved on. The 4 instances on this seedbox
#   are Sonarr v4.0.17 and Radarr v6.0/v6.1, which is enough API drift to make
#   buildarr crash before producing any useful state diff.
#
#   The patches in scripts/patches/ are the minimum surgical edits required to
#   make buildarr's from_remote/update_remote loop tolerant of the v4+ schema
#   shape. Each edit carries a `# QFlix patch 2026-05-11` marker so retiring
#   the patches is grep-able.
#
# WHAT'S PATCHED
#   buildarr-core:   base.py             — missing value/field → pydantic default
#                                          (was: raise ValueError)
#   buildarr-sonarr: import_lists.py     — languageProfileId guard + Trakt
#                                          required fields → Optional
#                    profiles/release.py — `preferred` + IPWR remote-map optional
#                                          (Sonarr v4 dropped both — uses Custom
#                                          Formats now)
#                    connect.py          — OnGrabField += indexer/custom_formats/
#                                          custom_format_score, OnImportField +=
#                                          custom_formats/custom_format_score
#   buildarr-radarr: media_management.py — ColonReplacement += smart
#                    notifications/discord.py — OnGrabField/OnImportField/
#                                          OnManualInteractionField += tags +
#                                          custom_formats/custom_format_score
#   radarr (SDK):    colon_replacement_format.py — ColonReplacementFormat += SMART
#
# IDEMPOTENT
#   For each patch target: skip if QFlix marker is already present; otherwise
#   `patch --dry-run` first and only apply on clean dry-run. Patches that don't
#   apply cleanly (e.g. upstream has caught up and edited the same area) are
#   left untouched and reported — that's the signal that this script is ready
#   to retire.
#
# REMOVAL PATH
#   When buildarr-sonarr and buildarr-radarr ship versions that natively
#   support Sonarr v4+ / Radarr v5+/v6:
#     1. ssh seedbox: ~/.apps/buildarr/.venv/bin/pip install -U \
#          buildarr buildarr-sonarr buildarr-radarr
#        (pip overwrites the venv files; QFlix markers go away.)
#     2. Re-run this script. Expected output: every patch reports "hunks do
#        not apply" — that's correct, upstream has the fixes now.
#     3. Run `systemctl --user start --wait buildarr.service` to confirm a
#        clean Result=success against the upgraded venv.
#     4. Delete this script, the 7 .patch files under scripts/patches/, and
#        flip the inventory.md "patched, working" bullet back to "managed
#        upstream".
#
# WHAT THIS DOES NOT DO
#   - Does not touch ~/.apps/buildarr/buildarr.yml. The populated instance
#     config (with API keys) is written by hand the first time buildarr is
#     configured to manage instances; the 50-buildarr-install.sh starter is
#     the commented-out template. The yml is preserved across re-runs.
#   - Does not restart buildarr.service / buildarr.timer. The patches apply
#     to .py files which are re-read every time buildarr is invoked.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/../lib/ssh.sh"
source "$HERE/../lib/log.sh"

PATCHES_DIR="$HERE/../patches"

# Ship patches to seedbox
log_info "Shipping patches to seedbox..."
sshm 'rm -rf /tmp/qflix-buildarr-patches && mkdir -p /tmp/qflix-buildarr-patches'
for pf in "$PATCHES_DIR"/buildarr-core-base.patch \
          "$PATCHES_DIR"/buildarr-sonarr-import_lists.patch \
          "$PATCHES_DIR"/buildarr-sonarr-release.patch \
          "$PATCHES_DIR"/buildarr-sonarr-connect.patch \
          "$PATCHES_DIR"/buildarr-radarr-media_management.patch \
          "$PATCHES_DIR"/buildarr-radarr-discord.patch \
          "$PATCHES_DIR"/radarr-sdk-colon_replacement_format.patch; do
  [ -f "$pf" ] || { echo "FATAL: missing patch $pf"; exit 1; }
  scpm_to "$pf" "/tmp/qflix-buildarr-patches/"
done

# Apply each patch to its target inside the venv. Idempotent.
sshm "bash -s" <<'REMOTE'
set -euo pipefail
TMP=/tmp/qflix-buildarr-patches
SP=$HOME/.apps/buildarr/.venv/lib/python3.11/site-packages
MARKER="QFlix patch 2026-05-11"

# Map: patch filename -> target path relative to site-packages
declare -A TARGET=(
  ["buildarr-core-base.patch"]="buildarr/config/base.py"
  ["buildarr-sonarr-import_lists.patch"]="buildarr_sonarr/config/import_lists.py"
  ["buildarr-sonarr-release.patch"]="buildarr_sonarr/config/profiles/release.py"
  ["buildarr-sonarr-connect.patch"]="buildarr_sonarr/config/connect.py"
  ["buildarr-radarr-media_management.patch"]="buildarr_radarr/config/settings/media_management.py"
  ["buildarr-radarr-discord.patch"]="buildarr_radarr/config/settings/notifications/discord.py"
  ["radarr-sdk-colon_replacement_format.patch"]="radarr/models/colon_replacement_format.py"
)

PATCHED=0; SKIPPED_MARKER=0; SKIPPED_NOAPPLY=0; SKIPPED_NOFILE=0; FAILED=0

cd "$SP"
for name in "${!TARGET[@]}"; do
  pf="$TMP/$name"
  rel="${TARGET[$name]}"
  [ -f "$pf" ] || { echo "  $name: patch file missing in /tmp staging dir, skipping"; continue; }
  if [ ! -f "$rel" ]; then
    echo "  $rel: target file not found (venv layout changed?), SKIPPING"
    SKIPPED_NOFILE=$((SKIPPED_NOFILE+1))
    continue
  fi
  if grep -q "$MARKER" "$rel"; then
    echo "  $rel: marker present, already patched"
    SKIPPED_MARKER=$((SKIPPED_MARKER+1))
    continue
  fi
  # Explicit target → patch ignores --- /+++ headers and applies hunks to $rel.
  if patch --dry-run -s "$rel" < "$pf" >/dev/null 2>&1; then
    if patch -s "$rel" < "$pf"; then
      echo "  $rel: patched OK"
      PATCHED=$((PATCHED+1))
    else
      echo "  $rel: dry-run succeeded but apply failed (?)"
      FAILED=$((FAILED+1))
    fi
  else
    echo "  $rel: patch hunks do not apply (upstream may have caught up). Leaving file alone."
    SKIPPED_NOAPPLY=$((SKIPPED_NOAPPLY+1))
  fi
done

# Bust bytecode cache so freshly-patched .py files are re-imported next run
find "$SP/buildarr" "$SP/buildarr_sonarr" "$SP/buildarr_radarr" "$SP/radarr" \
  -name '*.pyc' -delete 2>/dev/null || true

echo
echo "summary: $PATCHED patched, $SKIPPED_MARKER already-marked, $SKIPPED_NOAPPLY no-apply (upstream-improved?), $SKIPPED_NOFILE missing-target, $FAILED failed"
[ "$FAILED" -eq 0 ]
REMOTE

log_info "60-buildarr-patches complete"

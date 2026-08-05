#!/usr/bin/env bash
# scripts/configure/provision-admin-key.sh — mint the QFlix Admin phone key.
#
# ADDITIVE ONLY. This script APPENDS one authorized_keys line and never
# rewrites, filters or removes an existing one.
#
# The first draft of this script did a read-modify-write:
#     grep -v 'qflix-admin-phone' authorized_keys > ak.new
#     printf ... >> ak.new
#     mv ak.new authorized_keys
# which is a whole-file replace even when the content survives. The operator
# ruled that out on 2026-08-05 ("additive work only, do not delete any SSH keys
# or anything else until the new one is confirmed") and they were right: the
# rewrite is the step that can corrupt the file, and a corrupt authorized_keys
# costs you every key at once, not just the one being changed.
#
# Re-running this script therefore APPENDS A SECOND ENTRY rather than replacing
# the first. That is deliberate — removing the stale one is a separate,
# operator-authorised action. `--check` tells you if one is already present.
#
# THE REAL RISK is not the append; it is ending up with an unparseable
# authorized_keys and losing all SSH access. Mitigations, in order:
#   1. Hold an existing SSH session open in another terminal for the whole run.
#      If anything goes wrong that session is the only way back in.
#   2. A timestamped backup is taken before the append.
#   3. The append is verified: line count must rise by exactly one, and the
#      pre-existing lines must hash identically afterwards.
#   4. ssh-keygen -l must report exactly one more valid key than before.
# Any of those failing aborts before the key is trusted.
#
# The existing Heartbeat key (forced to app_status.py) is left ALONE. Both keys
# coexist: the old phone app keeps working while the new dispatcher surface is
# tested. Retiring the old entry is a later, separate decision.
set -uo pipefail

# Canonical host resolution (C2 fix). scripts/lib/ssh.sh is the one place in
# this repo that turns the bare FQDN in secrets/seedbox.ssh-host into a real
# SSH target by prefixing the operator's username — every other SSH caller in
# the repo goes through it. This script used to `cat` that file directly and
# skip the prefixing, so on a no-env-var run every ssh/scp below connected as
# the LOCAL username instead of the operator's — on Ultra.cc a wrong-user
# guess trips fail2ban, which also kills the tunnel: a lockout from the very
# script that exists to protect access. QFLIX_BOX still overrides it entirely
# for anyone who wants a different target.
# shellcheck source=../lib/ssh.sh
source "$(dirname "$0")/../lib/ssh.sh"
BOX="${QFLIX_BOX:-$SSHM_HOST}"
KEYDIR="${KEYDIR:-./.admin-key}"
KEY="$KEYDIR/qflix-admin"
REMOTE_CMD='command="python3 /home/'"${BOX%%@*}"'/scripts/mcp/dispatch.py",restrict'

if [ -z "$BOX" ]; then
  echo "no host: set QFLIX_BOX or create secrets/seedbox.ssh-host" >&2; exit 2
fi

# Defense in depth: whatever produced $BOX, refuse to proceed without a
# user@host shape. A bare FQDN here means every ssh/scp below would connect
# as the wrong user (see above), AND `${BOX%%@*}` in REMOTE_CMD would return
# the WHOLE STRING (no "@" to split on) instead of a username — the forced
# command would then point at /home/<fqdn>/scripts/mcp/dispatch.py, a path
# that cannot exist, and step 4's smoke test (which uses `~/`) would not
# catch it before the key is appended.
case "$BOX" in
  *@*) ;;
  *) echo "BOX must be user@host - got '$BOX' (secrets/seedbox.ssh-host holds the FQDN only)" >&2; exit 2 ;;
esac

if [ "${1:-}" = "--check" ]; then
  echo "existing qflix-admin-phone entries on the box:"
  ssh "$BOX" "grep -c 'qflix-admin-phone' ~/.ssh/authorized_keys || true"
  exit 0
fi

# --- 1. deploy the scripts first; a key pointed at a broken dispatcher is a lockout
echo "==> copying dispatcher + stARR scripts"
scp -q scripts/mcp/dispatch.py scripts/mcp/arr_library_peek.py \
       scripts/mcp/arr_disk_usage.py "$BOX:~/scripts/mcp/" || exit 1
ssh "$BOX" 'chmod +x ~/scripts/mcp/dispatch.py'

echo "==> smoke-testing dispatch.py over the EXISTING key"
if ! ssh "$BOX" 'python3 ~/scripts/mcp/dispatch.py help' | grep -q '"ok": true'; then
  echo "dispatcher does not run on the box - ABORTING before touching any key" >&2
  exit 1
fi

# --- 2. mint locally; refuse to clobber an existing private key
mkdir -p "$KEYDIR"
if [ -f "$KEY" ]; then
  echo "refusing to overwrite $KEY - move it aside first" >&2; exit 2
fi
ssh-keygen -t ed25519 -N '' -C 'qflix-admin-phone' -f "$KEY" >/dev/null
PUB=$(cat "$KEY.pub")

# --- 3. backup, append ONE line, verify additively
echo "==> appending (backup first, one line, no rewrite)"
ssh "$BOX" "PUB='$PUB' OPTS='$REMOTE_CMD' bash -s" <<'REMOTE'
set -u
AK=~/.ssh/authorized_keys
PRE_LINES=$(wc -l < "$AK")
PRE_HEAD=$(sha256sum < "$AK" | cut -d' ' -f1)
PRE_KEYS=$(ssh-keygen -l -f "$AK" 2>/dev/null | wc -l)
cp -p "$AK" "$AK.bak-preadmin-$(date -u +%Y%m%dT%H%M%SZ)"

printf '%s %s\n' "$OPTS" "$PUB" >> "$AK"

POST_LINES=$(wc -l < "$AK")
POST_HEAD=$(head -n "$PRE_LINES" "$AK" | sha256sum | cut -d' ' -f1)
POST_KEYS=$(ssh-keygen -l -f "$AK" 2>/dev/null | wc -l)
[ $((POST_LINES - PRE_LINES)) -eq 1 ] || { echo "line delta != 1 - RESTORE FROM BACKUP" >&2; exit 1; }
[ "$PRE_HEAD" = "$POST_HEAD" ] || { echo "existing lines CHANGED - RESTORE FROM BACKUP" >&2; exit 1; }
[ $((POST_KEYS - PRE_KEYS)) -eq 1 ] || { echo "valid-key delta != 1 - RESTORE FROM BACKUP" >&2; exit 1; }
echo "  lines $PRE_LINES -> $POST_LINES, prior lines unchanged, keys $PRE_KEYS -> $POST_KEYS"
REMOTE
[ $? -eq 0 ] || { echo "append verification failed - see message above" >&2; exit 1; }

# --- 4. prove the new key works before anyone relies on it
echo "==> verifying the new key on a fresh connection"
if ssh -i "$KEY" -o IdentitiesOnly=yes -o BatchMode=yes "$BOX" 'app.list' \
     | grep -q 'apps with a lifecycle'; then
  echo "  new key OK"
else
  echo "new key does NOT work - the backup is on the box, and your held session" >&2
  echo "is still open. Restore with:  cp ~/.ssh/authorized_keys.bak-preadmin-* ~/.ssh/authorized_keys" >&2
  exit 1
fi

ssh-keyscan -t ed25519 "${BOX#*@}" 2>/dev/null > "$KEYDIR/known_host"
echo "==> bundle ready in $KEYDIR (gitignored): qflix-admin, qflix-admin.pub, known_host"
echo "    Load into the app, then delete the private key from this machine."

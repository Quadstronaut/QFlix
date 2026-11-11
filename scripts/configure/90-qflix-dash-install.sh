#!/usr/bin/env bash
# Provision the QFlix Dashboard on the seedbox — STAGED. Installs + runs the app
# on its loopback port; does NOT touch the nginx root or Homarr (that's the
# cutover, scripts/configure/91-nginx-root-to-dash.sh). Idempotent. Runs from the
# workstation and SSHes in. Codifies the validated 2026-06-27 bring-up.
#
# Pre-req: ~/secrets/qflix-dash.discord_webhook must already exist on the box
# (operator-placed). The port/session_secret/plex_client_id are auto-generated.
#
# KEY ULTRA.CC GOTCHA — undici WASM OOM:
#   The slot caps `ulimit -v` ~10 GB (hard) but reports ~515 GB RAM, so Node
#   auto-sizes a huge heap and undici's WASM HTTP parser can't reserve its ~8 GB
#   trap guard region -> "Cannot allocate Wasm memory" crash on the first fetch().
#   Fix: NODE_OPTIONS=--disable-wasm-trap-handler (+ --max-old-space-size=512),
#   baked into the env file below. Applies to ANY Node app on this box that uses
#   global fetch(). See docs/superpowers/specs/2026-06-27-qflix-dashboard-design.md §3.
#   (Node 22 still accepts the flag inside NODE_OPTIONS - checked on 22.23.4.)
#
# NODE RUNTIME (QFLX-93): the dash runs its OWN pinned portable Node (versions.env
# QFLIX_DASH_NODE_VERSION/_SHA256, test-enforced) in ~/.apps/qflix-dash/bin/
# node-v<ver>, reached through the bin/node symlink the unit execs. Not nvm (it
# floated "latest 20"), and not Seerr's Node dir (a Seerr upgrade must never move
# the dash). SvelteKit 3 needs Node >= 22.17. The old nvm Node 20 is left on the
# box untouched: rollback = point bin/node at it and restart (see the PR body).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP="$HERE/apps/qflix-dash"
HOST="$(tr -d '[:space:]' < "$HERE/secrets/seedbox.ssh-host" 2>/dev/null \
        || tr -d '[:space:]' < "$HERE/secrets/seedbox.host")"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 "quadstronaut@$HOST")
DASH_NODE_VERSION="22.23.4"   # == versions.env QFLIX_DASH_NODE_VERSION (test-pinned)
DASH_NODE_SHA256="b8492284c76a54baf58d61668dd0e41ee0fc98adabf9503d30f05d08f55b4406"   # linux-x64.tar.gz

echo "[1/5] secrets (auto-gen) + pinned Node $DASH_NODE_VERSION"
# The pins travel as positional args ($1 $2) into the quoted heredoc.
"${SSH[@]}" "bash -l -s -- $DASH_NODE_VERSION $DASH_NODE_SHA256" <<'REMOTE'
set -e
[ -f ~/secrets/qflix-dash.port ]           || echo 42020 > ~/secrets/qflix-dash.port
[ -f ~/secrets/qflix-dash.session_secret ] || openssl rand -hex 32 > ~/secrets/qflix-dash.session_secret
[ -f ~/secrets/qflix-dash.plex_client_id ] || python3 -c "import uuid;print(uuid.uuid4())" > ~/secrets/qflix-dash.plex_client_id
[ -f ~/secrets/qflix-dash.discord_webhook ] || { echo "FATAL: place ~/secrets/qflix-dash.discord_webhook first" >&2; exit 1; }
chmod 600 ~/secrets/qflix-dash.*
mkdir -p ~/.apps/qflix-dash/logs ~/.config/qflix-dash ~/.config/systemd/user
V="$1"; WANT="$2"; B="$HOME/.apps/qflix-dash/bin"; ND="$B/node-v$V"
mkdir -p "$B"
# INVARIANT: bin/node (what the unit execs) only ever moves to a node-v<ver> that
# was just PROVEN to report v<ver>. Every failure path below exits with bin/node
# still on its old target, so a failed install never turns the next restart
# into a homepage outage. Every mv/ln carries its own `|| exit 1`: `set -e` is
# suspended inside an `a && b` list, which is how a failed first mv used to fall
# through to the flip.
T=""
trap 'rm -rf "$ND.part.$$" "$B/.node.$$"; [ -z "$T" ] || rm -rf "$T"' EXIT
rm -rf "$ND".part.*            # leftovers of a run killed before its trap fired
if [ -e "$ND" ] && [ "$("$ND/bin/node" --version 2>/dev/null)" != "v$V" ]; then
  # A stray dir (empty, half-moved, junk binary). Re-fetch it - unless it is the
  # LIVE target, which this script never deletes out from under the unit.
  [ "$(readlink "$B/node" 2>/dev/null)" != "node-v$V" ] \
    || { echo "FATAL: live target $ND is not a working Node v$V; fix by hand" >&2; exit 1; }
  echo "WARN: $ND is not a working Node v$V; removing and re-fetching" >&2
  rm -rf "$ND" || exit 1
fi
if [ ! -e "$ND" ]; then
  # Fetch + sha256 verify + extract in a scratch dir, then rename into place:
  # a half-extracted node-v<ver> never exists under bin/.
  T="$(mktemp -d)"; [ -d "$T" ] || { echo "FATAL: mktemp -d failed" >&2; exit 1; }
  curl -fsSL --retry 2 -o "$T/node.tgz" "https://nodejs.org/dist/v$V/node-v$V-linux-x64.tar.gz" || exit 1
  echo "$WANT  $T/node.tgz" | sha256sum -c --quiet - || { echo "FATAL: Node $V tarball sha256 mismatch" >&2; exit 1; }
  tar -xzf "$T/node.tgz" -C "$T" || exit 1
  [ -x "$T/node-v$V-linux-x64/bin/node" ] || { echo "FATAL: Node tarball has no bin/node" >&2; exit 1; }
  mv "$T/node-v$V-linux-x64" "$ND.part.$$" || exit 1
  # -T: rename ONTO $ND, never INTO it (a plain mv into an existing dir exits 0).
  mv -T "$ND.part.$$" "$ND" || exit 1
  FETCHED=1
fi
# Verify the binary BEFORE the flip, not after it.
HAVE="$("$ND/bin/node" --version 2>/dev/null)" || HAVE="(does not run)"
if [ "$HAVE" != "v$V" ]; then
  echo "FATAL: $ND/bin/node reports '$HAVE', want v$V; bin/node NOT flipped" >&2
  [ -z "${FETCHED:-}" ] || rm -rf "$ND"   # junk we fetched: next run re-fetches
  exit 1
fi
# Atomic flip of the symlink the unit execs. The running process keeps its
# already-loaded binary until step 4's restart.
ln -sfn "node-v$V" "$B/.node.$$" || exit 1
mv -Tf "$B/.node.$$" "$B/node" || exit 1
echo "node=$HAVE"
REMOTE

echo "[2/5] build (workstation) + ship"
( cd "$APP" && npm ci >/dev/null 2>&1 && npm run build >/dev/null 2>&1 )
scp -q -o BatchMode=yes -r "$APP/build" "$APP/package.json" "$APP/package-lock.json" "quadstronaut@$HOST":.apps/qflix-dash/
scp -q -o BatchMode=yes "$HERE/scripts/qflix-dash/plex_members.py"        "quadstronaut@$HOST":.apps/qflix-dash/
scp -q -o BatchMode=yes "$HERE/scripts/qflix-dash/qflix-dash.service.tmpl" "quadstronaut@$HOST":.apps/qflix-dash/

echo "[3/5] prod deps + env file + unit"
"${SSH[@]}" 'bash -l -s' <<'REMOTE'
set -e
cd ~/.apps/qflix-dash
export PATH="$HOME/.apps/qflix-dash/bin/node/bin:$PATH"   # the pinned Node's npm
npm ci --omit=dev >/dev/null 2>&1 || true   # app has no prod deps; harmless
# %h, not an absolute path: the unit execs the bin/node SYMLINK, so a Node
# change (or a rollback) is a symlink flip + restart, never a unit rewrite.
FQDN=$(cat ~/secrets/seedbox.host); NODE='%h/.apps/qflix-dash/bin/node/bin/node'
cat > ~/.config/qflix-dash/qflix-dash.env <<ENV
PORT=$(cat ~/secrets/qflix-dash.port)
HOST=127.0.0.1
PROTOCOL_HEADER=x-forwarded-proto
HOST_HEADER=x-forwarded-host
XFF_DEPTH=2
NODE_OPTIONS=--disable-wasm-trap-handler --max-old-space-size=512
UV_THREADPOOL_SIZE=2
MANITOBA_MAINT_BIN=$HOME/bin/manitoba-maint
PLEX_TOKEN=$(cat ~/secrets/plex.token)
PLEX_CLIENT_ID=$(cat ~/secrets/qflix-dash.plex_client_id)
PLEX_MEMBERS_PY=$HOME/.apps/python-plexapi/venv/bin/python $HOME/.apps/qflix-dash/plex_members.py
SEERR_URL=http://127.0.0.1:42011
SEERR_API_KEY=$(cat ~/secrets/jellyseerr.key)
DISCORD_WEBHOOK=$(cat ~/secrets/qflix-dash.discord_webhook)
SESSION_SECRET=$(cat ~/secrets/qflix-dash.session_secret)
Q_AVATAR_URL=https://raw.githubusercontent.com/Quadstronaut/QFlix/master/Q.png
FAQ_PROBE_URL=https://$FQDN/faq/
QFLIX_TOP_BIN=$HOME/scripts/qflix-top-pub.sh
ENV
chmod 600 ~/.config/qflix-dash/qflix-dash.env
sed "s#@@NODE@@#$NODE#g" ~/.apps/qflix-dash/qflix-dash.service.tmpl > ~/.config/systemd/user/qflix-dash.service
REMOTE

echo "[4/5] enable + RESTART (never 'enable --now' on a running service)"
# ROOT CAUSE OF THE 2026-07-29 DEAD-SHELL INCIDENT. `enable --now` STARTS a
# stopped unit but does NOT restart a running one. Step 2 above scp's a fresh
# build/ over the live process, so on every re-run this step used to leave the
# OLD node process serving the NEW assets: adapter-node hands static files to
# sirv, which snapshots its file manifest ONCE at process start. Files created
# after boot are invisible to it, and files rewritten in place keep the
# Content-Length/ETag/Last-Modified sirv computed at boot. Result on 2026-07-29:
# 6 of the 10 /_app/immutable modules the new shell referenced returned 404
# though every file was on disk at mode 644, the HTML document advertised a
# stale byte count while streaming fresh bytes, browsers aborted with
# net::ERR_CONTENT_LENGTH_MISMATCH, and the dashboard served ~22h of zero
# hydration with every monitor green.
# `restart` also starts a stopped unit, so it is a strict superset of `--now`
# and just as idempotent. Same reasoning as lib/recovery.py, which uses restart
# rather than start precisely because a process that is alive but degraded is a
# no-op under start.
"${SSH[@]}" 'systemctl --user daemon-reload && systemctl --user enable qflix-dash.service && systemctl --user restart qflix-dash.service'

echo "[5/5] verify (loopback) - /healthz AND the asset invariant"
# /healthz + /api/status are NOT a deploy verification. A stale in-memory server
# answers both happily -- that is exactly how the 2026-07-29 deploy reported
# success over a dead shell. The load-bearing assertion is that every
# /_app/immutable/* reference in the SERVED html resolves 200 from the RUNNING
# process, plus that the document's advertised Content-Length matches the bytes
# actually delivered. This is the same invariant the dash-asset-integrity canary
# asserts every 15 min; asserting it here means a deploy that leaves a dead
# shell FAILS the install instead of reporting success. Non-zero exit aborts the
# installer via the outer `set -e`.
"${SSH[@]}" 'bash -s' <<'REMOTE'
set -uo pipefail
P=$(cat ~/secrets/qflix-dash.port 2>/dev/null) || { echo "FATAL: ~/secrets/qflix-dash.port unreadable" >&2; exit 1; }
[ -n "$P" ] || { echo "FATAL: ~/secrets/qflix-dash.port empty" >&2; exit 1; }
BASE="http://127.0.0.1:$P"
BUILD="$HOME/.apps/qflix-dash/build/client"

# WAIT FOR READINESS FIRST. qflix-dash.service is Type=exec, so
# `systemctl --user restart` returns at execve, NOT at listen() - verified on the
# box, where ExecMainStartTimestamp and ActiveEnterTimestamp are the SAME
# instant while "Listening on http://127.0.0.1:<port>" lands in app.log later.
# Step 2 just scp'd a fresh 4.2 MB / 41-file server bundle, so those pages are
# guaranteed cold; bare node startup on this slot measured 961 ms cold vs 27 ms
# warm BEFORE loading build/index.js. A single-shot curl here therefore FATALs a
# perfectly good deploy on connection-refused. Every sibling installer settles
# first (91-nginx-root-to-dash.sh `sleep 2`, 80-vlogs-install.sh `sleep 3`,
# lib/recovery.py's post-restart backoff) and so does the canary's own
# reverify(); poll rather than sleep so a healthy deploy pays nothing.
HZ=000
for _ in $(seq 1 30); do
  # Same `|| echo 000` concatenation trap documented for the root fetch below:
  # curl PRINTS the status it received and the fallback APPENDS, so a refused
  # connection yields "000000" and a truncated 200 yields "200000". The loop
  # still behaves (neither equals "200"), but the FATAL below then reports a
  # nonsense code and sends the operator after the wrong fault. This heredoc
  # runs under `set -uo pipefail` with no -e, so a non-zero curl here does not
  # abort - normalise the value instead of bolting on a fallback.
  HZ=$(curl -s -m 5 -o /dev/null -w '%{http_code}' "$BASE/healthz")
  case "$HZ" in '' | *[!0-9]*) HZ=000 ;; esac
  [ "$HZ" = "200" ] && break
  sleep 2
done
# /healthz is still worth gating - it is the path the pusher's app probe uses
# (manifest health kind http_root, path_override /healthz), so an install that
# leaves it non-200 leaves the app monitor lying. It is just not SUFFICIENT: the
# whole point of this incident is that a stale process answers it happily.
[ "$HZ" = "200" ] || { echo "FATAL: /healthz returned HTTP $HZ after a 60s readiness budget" >&2; exit 1; }
echo "healthz=$(curl -s -m 5 "$BASE/healthz" | head -c 80)"
echo "status=$(curl -s -m 9 "$BASE/api/status" | head -c 160)"

HDR=$(mktemp); BODY=$(mktemp)
trap 'rm -f "$HDR" "$BODY"' EXIT
# `CODE=$(curl ... -w '%{http_code}' || echo 000)` CONCATENATES on a mid-transfer
# failure: curl prints the status it did receive AND the `|| echo` appends, so a
# truncated 200 came out as "200000" and this gate aborted with
# "loopback root returned HTTP 200000" -- the wrong diagnosis, and it aborted
# BEFORE the Content-Length predicate that exists specifically to name that
# fault. Capture the status and the code separately, and normalise.
CODE=$(curl -s -m 20 -D "$HDR" -o "$BODY" -w '%{http_code}' "$BASE/")
CURL_RC=$?
case "$CODE" in '' | *[!0-9]*) CODE=000 ;; esac
[ "$CODE" = "200" ] || { echo "FATAL: loopback root returned HTTP $CODE (curl rc=$CURL_RC)" >&2; exit 1; }

# Predicate 1 -- Content-Length agreement. Independent of the 404 sweep: sirv
# DID index these paths at boot, so a file rewritten in place at the same path
# still returns 200 under a stale length and the browser aborts the parse. An
# absent header (chunked / content-encoded) is inconclusive, not a failure.
# A non-zero curl rc alongside a 200 is itself the mismatch signature (curl 18,
# "transfer closed with outstanding read data remaining") -- the byte comparison
# below is what names it.
ADV=$(tr -d '\r' < "$HDR" | awk 'tolower($1)=="content-length:"{print $2}' | tail -1)
GOT=$(wc -c < "$BODY" | tr -d ' ')
if [ -n "${ADV:-}" ]; then
  [ "$ADV" = "$GOT" ] || { echo "FATAL: document Content-Length=$ADV but $GOT bytes delivered (curl rc=$CURL_RC) - stale sirv metadata; the running process predates this build" >&2; exit 1; }
  echo "content-length=agrees ($GOT bytes)"
elif [ "$CURL_RC" != "0" ]; then
  echo "FATAL: root returned 200 but the transfer failed (curl rc=$CURL_RC) with no Content-Length to check - the document did not arrive intact" >&2
  exit 1
else
  echo "content-length=absent (chunked/encoded) - predicate skipped"
fi

# Predicate 2 -- asset resolvability. Every /_app/immutable reference the shell
# emits must be servable by the process that emitted it.
REFS=$(grep -oE '/_app/immutable/[A-Za-z0-9._/-]+' "$BODY" | sort -u)
N=$(printf '%s\n' "$REFS" | grep -c . || true)
[ "${N:-0}" -ge 1 ] || { echo "FATAL: served html references 0 /_app/immutable assets - this is not a SvelteKit shell" >&2; exit 1; }
BAD=0; ABSENT=0
for ref in $REFS; do
  # Same concatenation trap as the root fetch above: normalise before comparing,
  # so a truncated asset is reported as "HTTP 200 (curl rc=18)" and not "200000".
  RC=$(curl -s -m 10 -o /dev/null -w '%{http_code}' "$BASE$ref")
  ARC=$?
  case "$RC" in '' | *[!0-9]*) RC=000 ;; esac
  [ "$RC" = "200" ] && [ "$ARC" = "0" ] && continue
  RC="$RC (curl rc=$ARC)"
  BAD=$((BAD+1))
  if [ -f "$BUILD$ref" ]; then
    echo "  $ref -> HTTP $RC  (file EXISTS on disk: stale in-process sirv manifest)" >&2
  else
    ABSENT=$((ABSENT+1))
    echo "  $ref -> HTTP $RC  (file ABSENT on disk: partial or broken deploy)" >&2
  fi
done
if [ "$BAD" -gt 0 ]; then
  echo "FATAL: $BAD/$N referenced assets do not resolve ($ABSENT absent on disk) - the shell cannot hydrate" >&2
  exit 1
fi
echo "assets=$N/$N resolve 200"
REMOTE
echo "Done - staged. Dashboard runs on loopback; cutover is scripts/configure/91-nginx-root-to-dash.sh."

#!/usr/bin/env bash
# tdarr-loopback-bind canary: is Tdarr still off the internet? (QFLX-96)
#
# Tdarr 2.17.01 has NO working auth (`auth`/`seededApiKey` are inert; a keyless
# POST /api/v2/cruddb answers 200), so WHERE IT LISTENS is its only access
# control. 50a-tdarr-server-loopback.py patches servers.js to bind 127.0.0.1.
# Anything that restores the vendor file undoes that silently at the next
# restart: a hand re-unzip, a restore from backup, a lifecycle zip_swap that
# predates its 50a post step. Nothing scheduled watched for it:
#   * the Kuma "Tdarr" probe hits /api/v2/status on 127.0.0.1, which a WILDCARD
#     listener also answers, so it stays green on a public bind;
#   * every other Tdarr canary curls loopback too, for the same reason;
#   * smoke `tdarr-loopback-bound` asserts exactly this, but no timer runs smoke.
#
# Two questions, because each failure looks fine to the other:
#   1. the RUNNING process: `ss` says every listener on serverPort is
#      127.0.0.1 (a marker cannot tell you the process took it);
#   2. the FILE: servers.js still carries QFLIX-LOOPBACK-BIND. A loopback
#      listener over an unpatched file is open at the NEXT restart, and the
#      pusher auto-heals Tdarr with restarts, so that is a red now, not later.
#
# Exit 0 loopback-only and patched /
#      1 tdarr-bind-public (listening on anything but 127.0.0.1) or
#        tdarr-loopback-patch-missing (file reverted, restart will open it) /
#      2 could-not-assert (config unreadable, ss failed, nothing listening,
#        servers.js unreadable). Nothing listening is "Tdarr"'s monitor to own,
#        and it must never read as a clean pass.
#
# Overrides (tests): QFLIX_CANARY_TDARR_LB_CONF, QFLIX_CANARY_TDARR_LB_SERVERS_JS,
# QFLIX_CANARY_TDARR_LB_SS (file of canned `ss -ltnH` output).
#
# Lives on the seedbox at ~/scripts/canaries/tdarr-loopback-bind.sh (deployed
# by 240-maintenance-install.sh). Invoked by
# manitoba-maint-canary-tdarr-loopback-bind, which pushes status=up/down to
# Kuma monitor "Canary Tdarr Loopback Bind".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
# shellcheck source=/dev/null
source "$ROOT/scripts/lib/ssh.sh"

# The body below sits inside sshm '...' and an UNQUOTED heredoc on the box:
# no apostrophes, no dollar signs, no backslashes in it.
RES=$(sshm '
python3 - <<PYEOF
import json, os, subprocess, sys

CONF = os.environ.get("QFLIX_CANARY_TDARR_LB_CONF") or os.path.expanduser(
    "~/.apps/tdarr/configs/Tdarr_Server_Config.json")
SRV = os.environ.get("QFLIX_CANARY_TDARR_LB_SERVERS_JS") or os.path.expanduser(
    "~/.apps/tdarr/Tdarr_Server/srcug/api/servers.js")
SS_FILE = os.environ.get("QFLIX_CANARY_TDARR_LB_SS")
MARK = "QFLIX-LOOPBACK-BIND"


def fail(stage, msg, rc):
    sys.stderr.write("STAGE=%s msg=%s%s" % (stage, msg, chr(10)))
    sys.exit(rc)


try:
    with open(CONF, encoding="utf-8") as fh:
        port = int(json.load(fh)["serverPort"])
except Exception:
    fail("tdarr-lb-config-unreadable",
         "no-readable-serverPort-in-Tdarr_Server_Config.json-cannot-assert-bind", 2)

if SS_FILE:
    with open(SS_FILE, encoding="utf-8") as fh:
        ss_out = fh.read()
else:
    try:
        ss_out = subprocess.run(["ss", "-ltnH", "sport = :%d" % port],
                                capture_output=True, text=True, timeout=20,
                                check=True).stdout
    except Exception:
        fail("tdarr-lb-ss-failed", "ss-could-not-list-listeners-cannot-assert-bind", 2)

# ss -ltnH columns: State Recv-Q Send-Q Local:Port Peer:Port
addrs = sorted({ln.split()[3] for ln in ss_out.splitlines() if len(ln.split()) >= 4})
want = "127.0.0.1:%d" % port
if not addrs:
    fail("tdarr-lb-not-listening",
         "nothing-listening-on-serverPort-%d-cannot-assert-bind-Tdarr-monitor-owns-liveness" % port, 2)
public = [a for a in addrs if a != want]
if public:
    fail("tdarr-bind-public",
         "listening-on-%s-not-127.0.0.1-only-keyless-admin-API-reachable-re-run-50a-then-restart-tdarr-server"
         % ",".join(public), 1)

try:
    with open(SRV, encoding="utf-8") as fh:
        marks = fh.read().count(MARK)
except OSError:
    fail("tdarr-lb-servers-js-unreadable", "cannot-read-servers.js-cannot-assert-patch", 2)
if marks < 1:
    fail("tdarr-loopback-patch-missing",
         "bound-to-loopback-NOW-but-servers.js-lost-%s-the-next-restart-binds-the-wildcard-re-run-50a" % MARK, 1)

print("PASS: tdarr-loopback-bind - listening on %s only, servers.js patched" % want)
PYEOF
')
RC=$?
echo "$RES"
exit $RC

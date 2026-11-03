#!/usr/bin/env bash
# tdarr-loopback-bind canary: can anyone but us reach Tdarr? (QFLX-96, QFLX-97)
#
# Tdarr 2.17.01 has NO working auth (`auth`/`seededApiKey` are inert; a keyless
# POST /api/v2/cruddb answers 200, and its plugin/ffmpeg routes run code as our
# uid), so WHO CAN CONNECT is its only access control. This slot shares one
# network namespace with every tenant, so a TCP listener on 127.0.0.1 is open
# to ~150 other uids. The model since QFLX-97:
#   * Tdarr listens on a unix socket in /run/user/<uid>/qflix-tdarr (0700);
#     50a patches servers.js to do that (marker QFLIX-UNIX-SOCKET) and
#     mainApi.js to keep the UI working on it (QFLIX-WEBUI-SHARED-IO);
#   * tdarr-uid-relay owns 127.0.0.1:<serverPort> and forwards only peers the
#     kernel attributes to our uid.
#
# Name kept from QFLX-96 because the Kuma monitor and timer carry it.
#
# What it asserts, each because the others cannot see it:
#   1. the ONLY listener on serverPort is 127.0.0.1 and is the relay's MainPID
#      (anything else is Tdarr back on TCP, or a squatter while the relay is
#      down: the Node would hand its jobs to whoever holds the port);
#   2. no Tdarr process owns ANY TCP listener;
#   3. the socket directory is ours and 0700, and the socket exists;
#   4. both patch markers are in the files (a reverted file is open at the
#      NEXT restart, and the pusher auto-heals Tdarr with restarts);
#   5. the RUNNING relay enforces: its heartbeat is fresh, from the unit's
#      MainPID, allows exactly our uid, is not in a test mode, passed its own
#      kernel-lookup selftest, and runs the code that is on disk;
#   6. the code on disk refuses a foreign uid and a failed lookup
#      (tdarr-uid-relay.py --selftest, throwaway sockets only).
#
# Exit 0 all hold / 1 any red (named by STAGE=) / 2 could-not-assert
# (config unreadable, ss failed). A dead relay is RED here, not "cannot
# assert": the relay is exactly what this canary owns.
#
# Overrides (tests): QFLIX_CANARY_TDARR_LB_CONF, _SERVERS_JS, _MAINAPI_JS,
# _SS (canned `ss -ltnpH sport = :port`), _SS_ALL (canned `ss -ltnpH`),
# _RELAY_PID, _SOCK_DIR, _STATE, _RELAY (script path; "skip" skips 6).
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
import hashlib, json, os, stat, subprocess, sys, time

E = os.environ.get
HOME = os.path.expanduser("~")
UID = os.getuid()
CONF = E("QFLIX_CANARY_TDARR_LB_CONF") or HOME + "/.apps/tdarr/configs/Tdarr_Server_Config.json"
API = HOME + "/.apps/tdarr/Tdarr_Server/srcug/api/"
SRV = E("QFLIX_CANARY_TDARR_LB_SERVERS_JS") or API + "servers.js"
MAIN = E("QFLIX_CANARY_TDARR_LB_MAINAPI_JS") or API + "mainApi.js"
SOCK_DIR = E("QFLIX_CANARY_TDARR_LB_SOCK_DIR") or "/run/user/%d/qflix-tdarr" % UID
STATE = E("QFLIX_CANARY_TDARR_LB_STATE") or SOCK_DIR + "/relay.json"
RELAY = E("QFLIX_CANARY_TDARR_LB_RELAY") or HOME + "/scripts/ops/tdarr-uid-relay.py"
STALE_S = 300
NL = chr(10)


def fail(stage, msg, rc):
    sys.stderr.write("STAGE=%s msg=%s%s" % (stage, msg, NL))
    sys.exit(rc)


def run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60, check=True).stdout


def canned_or_run(var, argv, stage):
    path = E(var)
    try:
        if path:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        return run(argv)
    except Exception:
        fail(stage, "ss-could-not-list-listeners-cannot-assert", 2)


def pids(line):
    out = set()
    for part in line.split("pid=")[1:]:
        digits = ""
        for ch in part:
            if not ch.isdigit():
                break
            digits += ch
        if digits:
            out.add(int(digits))
    return out


try:
    with open(CONF, encoding="utf-8") as fh:
        port = int(json.load(fh)["serverPort"])
except Exception:
    fail("tdarr-lb-config-unreadable",
         "no-readable-serverPort-in-Tdarr_Server_Config.json-cannot-assert", 2)

relay_pid = E("QFLIX_CANARY_TDARR_LB_RELAY_PID")
if relay_pid is None:
    try:
        relay_pid = run(["systemctl", "--user", "show", "-p", "MainPID", "--value",
                         "tdarr-uid-relay.service"]).strip()
    except Exception:
        relay_pid = "0"
relay_pid = int(relay_pid or 0)

# 1. the port: 127.0.0.1 only, held by the relay and nothing else
lines = [ln for ln in canned_or_run("QFLIX_CANARY_TDARR_LB_SS",
         ["ss", "-ltnpH", "sport = :%d" % port], "tdarr-lb-ss-failed").splitlines()
         if len(ln.split()) >= 4]
want = "127.0.0.1:%d" % port
if not lines:
    fail("tdarr-relay-down",
         "nothing-listening-on-%d-tdarr-uid-relay-is-down-Node-and-API-callers-cut-off" % port, 1)
public = sorted({ln.split()[3] for ln in lines} - {want})
if public:
    fail("tdarr-bind-public", "listening-on-%s-not-127.0.0.1-only" % ",".join(public), 1)
owners = set()
for ln in lines:
    owners |= pids(ln)
if relay_pid <= 0 or owners != {relay_pid}:
    fail("tdarr-port-not-relay",
         "port-%d-owned-by-%s-not-tdarr-uid-relay-%d-Tdarr-back-on-TCP-or-a-squatter"
         % (port, sorted(owners) or "an-invisible-pid", relay_pid), 1)

# 2. no Tdarr process owns any TCP listener
allss = canned_or_run("QFLIX_CANARY_TDARR_LB_SS_ALL", ["ss", "-ltnpH"], "tdarr-lb-ss-failed")
tdarr_tcp = sorted({ln.split()[3] for ln in allss.splitlines()
                    if chr(34) + "Tdarr_" in ln and len(ln.split()) >= 4})
if tdarr_tcp:
    fail("tdarr-own-tcp-listener",
         "Tdarr-owns-TCP-%s-reachable-by-every-tenant-re-run-50a-then-restart-tdarr-server"
         % ",".join(tdarr_tcp), 1)

# 3. the socket directory: ours, a directory, 0700; the socket is there
try:
    st = os.lstat(SOCK_DIR)
except OSError:
    fail("tdarr-socket-dir-missing", "no-%s-Tdarr-is-not-on-its-unix-socket" % SOCK_DIR, 1)
if not stat.S_ISDIR(st.st_mode) or st.st_uid != UID or stat.S_IMODE(st.st_mode) != 0o700:
    fail("tdarr-socket-dir-mode",
         "%s-is-mode-%o-uid-%d-expected-0700-ours" % (SOCK_DIR, stat.S_IMODE(st.st_mode), st.st_uid), 1)
try:
    if not stat.S_ISSOCK(os.lstat(SOCK_DIR + "/tdarr.sock").st_mode):
        raise OSError
except OSError:
    fail("tdarr-socket-missing", "no-tdarr.sock-in-%s" % SOCK_DIR, 1)

# 4. the patch markers
for path, mark, need in ((SRV, "QFLIX-UNIX-SOCKET", 2), (MAIN, "QFLIX-WEBUI-SHARED-IO", 1)):
    try:
        with open(path, encoding="utf-8") as fh:
            n = fh.read().count(mark)
    except OSError:
        fail("tdarr-lb-patch-unreadable", "cannot-read-%s-cannot-assert-patch" % path, 2)
    if n < need:
        fail("tdarr-unix-patch-missing",
             "%s-lost-%s-the-next-restart-reverts-Tdarr-re-run-50a" % (os.path.basename(path), mark), 1)

# 5. the running relay enforces
try:
    with open(STATE, encoding="utf-8") as fh:
        hb = json.load(fh)
    if not isinstance(hb, dict):
        raise ValueError
except Exception:
    fail("tdarr-relay-heartbeat-missing", "no-readable-relay.json-the-relay-is-not-reporting", 1)
age = time.time() - float(hb.get("ts", 0))
if age > STALE_S:
    fail("tdarr-relay-heartbeat-stale", "relay-heartbeat-%ds-old" % age, 1)
if hb.get("pid") != relay_pid:
    fail("tdarr-relay-heartbeat-foreign", "heartbeat-pid-%s-is-not-unit-MainPID-%d" % (hb.get("pid"), relay_pid), 1)
if hb.get("allow_uid") != UID or hb.get("refuse_own") is not False:
    fail("tdarr-relay-not-enforcing",
         "relay-allows-uid-%s-refuse_own-%s-expected-%d-False" % (hb.get("allow_uid"), hb.get("refuse_own"), UID), 1)
if hb.get("selftest") != "pass":
    fail("tdarr-relay-lookup-broken",
         "relay-cannot-read-peer-uids-%s-it-refuses-everything" % str(hb.get("selftest"))[:80].replace(" ", "-"), 1)

# 6. the code on disk refuses a foreign uid and a failed lookup
if RELAY != "skip":
    try:
        with open(RELAY, "rb") as fh:
            disk = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        fail("tdarr-relay-code-missing", "cannot-read-%s" % RELAY, 1)
    if hb.get("code_sha256") != disk:
        fail("tdarr-relay-stale-code", "running-relay-is-not-the-code-on-disk-restart-tdarr-uid-relay", 1)
    st_run = subprocess.run([sys.executable, RELAY, "--selftest"], capture_output=True, text=True, timeout=60)
    if st_run.returncode != 0:
        bad = [ln for ln in st_run.stdout.splitlines() if ln.startswith("FAIL")]
        fail("tdarr-relay-selftest-failed", "-".join((bad or ["no-output"])[0].split())[:120], 1)

print("PASS: tdarr-loopback-bind - %s held by tdarr-uid-relay pid %d (uid %d only, %s accepted / %s refused), "
      "Tdarr on 0700 unix socket, no Tdarr TCP listener, patched"
      % (want, relay_pid, UID, hb.get("accepted"), int(hb.get("refused_foreign", 0)) + int(hb.get("refused_lookup", 0))))
PYEOF
')
RC=$?
echo "$RES"
exit $RC

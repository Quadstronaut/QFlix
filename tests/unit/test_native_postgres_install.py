"""scripts/configure/312-native-postgres-install.sh (QFLX-37, UCC divorce A13).

Subprocess tests against fakes that MODEL the slot, not just record argv:

  * a fake /proc: the UCC postmaster (cmdline "postgres", PG_VERSION in its
    environ) and its checkpointer live in a docker cgroup; the native postmaster
    lives in the qflix-postgres.service cgroup;
  * a fake postgres toolchain shipped INSIDE the fake .debs (dpkg-deb -x is a
    tar), so --install lays out the very binaries the later modes run. Each
    "server" is a directory: dbs/<db>/{counts,seqs,owner}. The UCC server
    answers on 127.0.0.1:42009 only while the container runs (and only with a
    pgpass file); a native/proof cluster answers on its unix socket dir only
    while pg_ctl / the unit runs it. pg_dump/pg_restore really carry the rows,
    so "counts equal after restore" is observed, not asserted by a stub;
  * fake ss / systemctl / appctl / crontab / pgrep / ps / ldd / curl / http
    that mutate that state the way the real tools would.

Real python runs pg_native.py, swapstate.py and suppression.py, so the snapshot
JSON, swap state and push-suppress.json are the real files.

Documentation IPs only (203.0.113.0/24): never a real slot address.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "312-native-postgres-install.sh"
GOLDEN_UNIT = REPO / "scripts" / "maint" / "systemd" / "qflix-postgres.service"
UNIT = "qflix-postgres.service"
VER = "17.11-1.pgdg13+2"
PW = "fixture-pw-not-real"
PUB = "203.0.113.7"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True, text=True).stdout.strip()


def _masked(p: Path) -> bool:
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


# --- the fake toolchain (lands in bin/<ver>/usr/lib/postgresql/17/bin) ----------------

PRELUDE = r'''#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$CALLS/calls.log"
H=""; P=""; D=""; C=""; F=""; DD=""; U=""; POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h) H="$2"; shift ;; -p) P="$2"; shift ;; -d) D="$2"; shift ;;
    -c) C="$2"; shift ;; -f) F="$2"; shift ;; -D) DD="$2"; shift ;;
    -U) U="$2"; shift ;; -v|-l|-m|-t|-o|-E) shift ;;
    -*) ;;
    *) POS+=("$1") ;;
  esac
  shift
done
# srv: the server root this connection reaches, or fail like libpq would.
srv() {
  if [ -d "$H" ]; then
    [ -f "$H/.fake-data" ] || { echo "connection to socket $H failed" >&2; exit 2; }
    echo "$(cat "$H/.fake-data")/dbs"
  elif [ "$H" = 127.0.0.1 ] && [ "$P" = 42009 ]; then
    [ -d "$FAKE_PROC/9001" ] || { echo "connection refused" >&2; exit 2; }
    grep -q ':fixture-pw-not-real$' "${PGPASSFILE:-/nonexistent}" 2>/dev/null \
      || { echo "password authentication failed" >&2; exit 2; }
    echo "$FAKE_UCC/dbs"
  else
    echo "no server at $H:$P" >&2; exit 2
  fi
}
'''

FAKE_PSQL = PRELUDE + r'''
S="$(srv)" || exit 2
if [ -n "$F" ]; then
  grep -q "ALTER ROLE" "$F" || { echo "no roles" >&2; exit 3; }
  grep -q "^CREATE ROLE quadstronaut;" "$F" && { echo 'ERROR: role "quadstronaut" already exists' >&2; exit 3; }
  touch "$S/../roles"; exit 0
fi
db="$S/${D:-postgres}"
case "$C" in
  *"/*qflix:dbs*/"*) ls "$S" ;;
  *"/*qflix:dbowners*/"*) for d in $(ls "$S"); do echo "$d|$(cat "$S/$d/owner")"; done ;;
  *"/*qflix:counts*/"*)
    [ -d "$db" ] || exit 2
    cat "$db/counts"
    if [ "${FAKE_UCC_WRITES:-0}" = 1 ] && [ "$S" = "$FAKE_UCC/dbs" ] && [ "$D" = listmonk ]; then
      awk -F'|' -v OFS='|' '$1=="public.subscribers"{$2=$2+1} {print}' "$db/counts" > "$db/counts.n" && mv "$db/counts.n" "$db/counts"
    fi ;;
  *"/*qflix:seqs*/"*) [ -d "$db" ] || exit 2; cat "$db/seqs" ;;
  *"/*qflix:running*/"*) echo "${FAKE_RUNNING:-0}" ;;
  *"/*qflix:campaigns*/"*) awk -F'|' '$1=="public.campaigns"{print $2; f=1} END{if(!f)print 0}' "$db/counts" ;;
  *"/*qflix:sanitize*/"*) touch "$db/sanitized" ;;
  *"/*qflix:sanitize-check*/"*)
    if [ -f "$db/sanitized" ] && [ "${FAKE_SANITIZE_LEFT:-0}" = 0 ]; then echo 0; else echo 3; fi ;;
  *"/*qflix:createdb*/"*)
    n="$(printf '%s' "$C" | sed -n 's/.*create database "\([A-Za-z0-9_]*\)" owner "\([A-Za-z0-9_]*\)".*/\1 \2/p')"
    set -- $n
    [ -n "${1:-}" ] || exit 3
    [ -d "$S/$1" ] && { echo "database exists" >&2; exit 3; }
    mkdir -p "$S/$1"; : > "$S/$1/counts"; : > "$S/$1/seqs"; echo "$2" > "$S/$1/owner" ;;
  "select 1") echo 1 ;;
  *) echo "fake psql: unknown sql: $C" >&2; exit 3 ;;
esac
'''

FAKE_PG_DUMP = PRELUDE + r'''
S="$(srv)" || exit 2
[ -d "$S/$D" ] || { echo "no db $D" >&2; exit 1; }
{
  if [ "${FAKE_DUMP_LOSES_ROW:-0}" = 1 ] && [ "$D" = listmonk ]; then
    awk -F'|' -v OFS='|' '$1=="public.subscribers"{$2=$2-1} {print}' "$S/$D/counts"
  else cat "$S/$D/counts"; fi
  echo "@@SEQS@@"; cat "$S/$D/seqs"
} > "$F"
'''

FAKE_PG_DUMPALL = PRELUDE + r'''
S="$(srv)" || exit 2
if [ "${FAKE_GLOBALS_NOPASS:-0}" = 1 ]; then
  printf 'CREATE ROLE quadstronaut;\nALTER ROLE quadstronaut WITH SUPERUSER LOGIN;\n' > "$F"
else
  printf "CREATE ROLE quadstronaut;\nALTER ROLE quadstronaut WITH SUPERUSER INHERIT LOGIN PASSWORD 'SCRAM-SHA-256\$4096:a\$b:c';\n" > "$F"
fi
'''

FAKE_PG_RESTORE = PRELUDE + r'''
[ "${FAKE_RESTORE_FAILS:-0}" = 1 ] && { echo "pg_restore: error" >&2; exit 1; }
S="$(srv)" || exit 2
[ -d "$S/$D" ] || { echo "no db $D" >&2; exit 1; }
f="${POS[0]}"
awk '/^@@SEQS@@$/{s=1; next} !s' "$f" > "$S/$D/counts"
awk '/^@@SEQS@@$/{s=1; next} s' "$f" > "$S/$D/seqs"
'''

FAKE_INITDB = PRELUDE + r'''
[ -n "$DD" ] || exit 1
mkdir -p "$DD/dbs/postgres"
echo 17 > "$DD/PG_VERSION"
echo "# initdb default" > "$DD/postgresql.conf"
: > "$DD/dbs/postgres/counts"; : > "$DD/dbs/postgres/seqs"; echo "$U" > "$DD/dbs/postgres/owner"
'''

FAKE_PG_CTL = PRELUDE + r'''
verb="${POS[${#POS[@]}-1]}"
conf="$DD/postgresql.conf"
sock="$(sed -n "s/^unix_socket_directories = '\(.*\)'$/\1/p" "$conf" | tail -n 1)"
mark="$CALLS/running-$(printf '%s' "$DD" | cksum | cut -d' ' -f1)"
case "$verb" in
  start)
    [ "${FAKE_PGCTL_FAIL:-0}" = 1 ] && exit 1
    [ -d "$sock" ] || { echo "no socket dir $sock" >&2; exit 1; }
    echo "pg_ctl-start listen=$(sed -n "s/^listen_addresses = '\(.*\)'$/\1/p" "$conf" | tail -n 1)" >> "$CALLS/calls.log"
    echo "$DD" > "$sock/.fake-data"; touch "$mark" ;;
  stop) rm -f "$sock/.fake-data" "$mark" ;;
esac
'''

FAKE_POSTGRES = r'''#!/usr/bin/env bash
[ "${1:-}" = --version ] && { echo "postgres (PostgreSQL) ${FAKE_PG_VER:-17.11} (Debian 17.11-1.pgdg13+2)"; exit 0; }
exec sleep 300
'''

SERVER_BINS = {"postgres": FAKE_POSTGRES, "initdb": FAKE_INITDB, "pg_ctl": FAKE_PG_CTL}
CLIENT_BINS = {"psql": FAKE_PSQL, "pg_dump": FAKE_PG_DUMP, "pg_dumpall": FAKE_PG_DUMPALL,
               "pg_restore": FAKE_PG_RESTORE}

FAKE_LISTMONK = r'''#!/usr/bin/env bash
echo "listmonk $*" >> "$CALLS/calls.log"
cfg=""; while [ $# -gt 0 ]; do [ "$1" = --config ] && cfg="$2"; shift; done
grep -q '^address = "127.0.0.1:' "$cfg" || exit 9
cp "$cfg" "$CALLS/scratch-listmonk.toml"
touch "$CALLS/scratch-lm"
exec sleep 300
'''

CRONTAB_FIXTURE = (
    "*/5 * * * * /home/u/scripts/ops/heartbeat-listmonk.sh\n"
    "0 4 * * * /usr/bin/python3 /home/u/scripts/ops/listmonk-sync.py >> /home/u/x.log 2>&1\n"
    "* * * * * /home/u/scripts/plex/stream_stats.sh\n"
)

LM_CONFIG = f"""[app]
address = "127.0.0.1:42014"

[db]
host = "127.0.0.1"
port = 42009
user = "quadstronaut"
password = "{PW}"
database = "listmonk"
ssl_mode = "disable"
"""

UCC_DBS = {
    "listmonk": ("public.campaigns|73\npublic.settings|70\npublic.subscribers|12\n",
                 "public.campaigns_id_seq|80\npublic.subscribers_id_seq|15\n"),
    "jfstat": ("public.jf_items|17\n", "public.jf_items_id_seq|null\n"),
    "postgres": ("", ""),
}

SATURDAY = "1791626400"   # 2026-10-10T10:00:00Z: far from the Monday newsletter
MONDAY = "1791817200"     # 2026-10-12T15:00:00Z: the send


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, body in files.items():
            data = body.encode()
            ti = tarfile.TarInfo(f"usr/lib/postgresql/17/bin/{name}")
            ti.size, ti.mode = len(data), 0o755
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


class Box:
    """A fake slot. Paths are POSIX strings for bash."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "pg-native"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.secrets = self.home / "secrets"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls"
        self.ucc = tmp / "ucc-server"
        self.manifest = self.state / "apps.yaml"
        self.lmconf = self.apps / "listmonk" / "etc" / "config.toml"
        for d in (self.unitdir, self.envdir, self.swap, self.secrets, self.proc,
                  self.stub, self.calls, self.lmconf.parent, self.apps / "listmonk" / "bin",
                  self.apps / "postgres" / "data"):
            d.mkdir(parents=True, exist_ok=True)
        self.lmconf.write_text(LM_CONFIG, newline="\n")
        (self.secrets / "postgres.port").write_text("42009\n")
        (self.secrets / "listmonk.api_user").write_text("api\n")
        (self.secrets / "listmonk.api_token").write_text("tok\n")
        for db, (counts, seqs) in UCC_DBS.items():
            d = self.ucc / "dbs" / db
            d.mkdir(parents=True)
            (d / "counts").write_text(counts, newline="\n")
            (d / "seqs").write_text(seqs, newline="\n")
            (d / "owner").write_text("quadstronaut\n", newline="\n")
        (self.calls / "crontab").write_text(CRONTAB_FIXTURE, newline="\n")
        (self.calls / "lm-active").write_text("")
        lm = self.apps / "listmonk" / "bin" / "listmonk"
        lm.write_text(FAKE_LISTMONK, newline="\n")
        lm.chmod(0o755)
        self.uid = _uid()
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._debs()
        self._stubs()

    # --- state ---------------------------------------------------------------
    def _proc(self, pid: int, cgroup: str, argv: list[str], environ: dict | None = None):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{self.uid}\t{self.uid}\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        env = environ or {}
        (d / "environ").write_bytes(b"".join(f"{k}={v}".encode() + b"\0" for k, v in env.items()))

    def container_up(self, build=VER):
        self._proc(9001, "0::/system.slice/docker-abc.scope", ["postgres"],
                   {"PG_MAJOR": "17", "PG_VERSION": build, "LANG": "en_US.utf8"})
        self._proc(9003, "0::/system.slice/docker-abc.scope", ["postgres: checkpointer "])

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def listmonk_active(self) -> bool:
        return (self.calls / "lm-active").exists()

    def crontab(self) -> str:
        return (self.calls / "crontab").read_text()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  postgres:", f"    class: {cls}", "    ucc_slug: postgres"]
        if cls == "systemd":
            lines.append(f"    unit: {UNIT}")
        if dormant:
            lines.append("    ucc_dormant: true")
        if swap_state:
            lines.append(f"    swap_state: {swap_state}")
        self.manifest.write_text("\n".join(lines) + "\n", newline="\n")

    def calls_text(self) -> str:
        p = self.calls / "calls.log"
        return p.read_text() if p.exists() else ""

    def suppressed(self) -> dict:
        p = self.state / "push-suppress.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def swapstate(self) -> dict:
        p = self.swap / "postgres" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def native_counts(self, db="listmonk") -> str:
        return (self.appdir / "data" / "dbs" / db / "counts").read_text()

    # --- fakes ---------------------------------------------------------------
    def _debs(self):
        self.server_deb = self.tmp / "server.deb"
        self.client_deb = self.tmp / "client.deb"
        self.server_deb.write_bytes(_tgz(SERVER_BINS))
        self.client_deb.write_bytes(_tgz(CLIENT_BINS))
        self.server_sha = hashlib.sha256(self.server_deb.read_bytes()).hexdigest()
        self.client_sha = hashlib.sha256(self.client_deb.read_bytes()).hexdigest()

    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C = _posix(self.proc), _posix(self.calls)
        U = _posix(self.unitdir)
        run, data = _posix(self.appdir / "run"), _posix(self.appdir / "data")
        mkproc = r'''mkproc() {  # pid cgroup argv0
  mkdir -p "%(P)s/$1"
  printf 'Name:\tx\nUid:\t%%s\t%%s\n' "$(id -u)" "$(id -u)" > "%(P)s/$1/status"
  echo "$2" > "%(P)s/$1/cgroup"
  printf '%%s\0' "${@:3}" > "%(P)s/$1/cmdline"
  : > "%(P)s/$1/environ"
}
''' % {"P": P}
        self._w("appctl", mkproc + f'''echo "appctl $*" >> "{C}/calls.log"
case "$1" in
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001" "{P}/9003" ;;
  start) mkproc 9001 "0::/system.slice/docker-abc.scope" postgres
         printf 'PG_VERSION={VER}\\0' > "{P}/9001/environ"
         mkproc 9003 "0::/system.slice/docker-abc.scope" "postgres: checkpointer " ;;
  is-native) v="${{FAKE_ISNATIVE:-ucc}}"; echo "$v"; [ "$v" = native ] ;;
  version) echo "Unknown command: version"; exit 1 ;;
esac
''')
        self._w("systemctl", mkproc + f'''echo "systemctl $*" >> "{C}/calls.log"
[ "$1" = --user ] && shift
case "$1:${{2:-}}:${{3:-}}" in
  is-active:listmonk.service:*) [ -f "{C}/lm-active" ] && exit 0; exit 3 ;;
  stop:listmonk.service:*) rm -f "{C}/lm-active" ;;
  start:listmonk.service:*) [ "${{FAKE_LM_FAILS:-0}}" = 1 ] || touch "{C}/lm-active" ;;
  is-active:{UNIT}:*) [ -d "{P}/9002" ] && exit 0; exit 3 ;;
  enable:--now:{UNIT})
    {{ [ -L "{U}/{UNIT}" ] || [ ! -s "{U}/{UNIT}" ]; }} && {{ echo "unit masked or missing" >&2; exit 1; }}
    [ "${{FAKE_NATIVE_FAILS:-0}}" = 1 ] && exit 0
    mkproc 9002 "0::/user.slice/user-1.slice/app.slice/{UNIT}" /h/.apps/pg-native/bin/current/usr/lib/postgresql/17/bin/postgres -D x
    echo "{data}" > "{run}/.fake-data"
    sed -n "s/^listen_addresses = '\\(.*\\)'$/\\1/p" "{data}/postgresql.conf" | tail -n 1 > "{C}/native-listen" ;;
  stop:{UNIT}:*) rm -rf "{P}/9002"; rm -f "{run}/.fake-data" "{C}/native-listen" ;;
  mask:{UNIT}:*) [ -s "{U}/{UNIT}" ] && [ ! -L "{U}/{UNIT}" ] && {{ echo "Failed to mask unit: File exists." >&2; exit 1; }}
        ln -sf /dev/null "{U}/{UNIT}" ;;
  unmask:{UNIT}:*) {{ [ -L "{U}/{UNIT}" ] || [ ! -s "{U}/{UNIT}" ]; }} && rm -f "{U}/{UNIT}" ;;
esac
exit 0
''')
        self._w("ss", f'''
if [ -d "{P}/9001" ]; then
  for a in ${{FAKE_UCC_LISTEN:-127.0.0.1 172.17.0.1 {PUB}}}; do echo "LISTEN 0 65535 $a:42009 0.0.0.0:*"; done
fi
if [ -d "{P}/9002" ] && [ -s "{C}/native-listen" ]; then
  for a in $(tr ',' ' ' < "{C}/native-listen"); do echo "LISTEN 0 244 $a:42009 0.0.0.0:*"; done
fi
[ -f "{C}/lm-active" ] && echo "LISTEN 0 4096 127.0.0.1:42014 0.0.0.0:*"
exit 0
''')
        self._w("ps", f'n=${{FAKE_TASKS:-1000}}; n=$((n + 20 * $(ls "{C}" | grep -c "^running-") ))\n'
                      f'[ -f "{C}/scratch-lm" ] && n=$((n + 40))\n'
                      'for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("pgrep", f'''echo "pgrep $*" >> "{C}/calls.log"
case "$*" in
  *listmonk-sync*) [ "${{FAKE_SYNC_RUNNING:-0}}" = 1 ] && {{ echo 4242; exit 0; }} ;;
esac
exit 1
''')
        self._w("crontab", f'''echo "crontab $*" >> "{C}/calls.log"
case "$1" in
  -l) [ "${{FAKE_CRONTAB_FAIL:-0}}" = 1 ] && exit 1; cat "{C}/crontab" ;;
  -) cat > "{C}/crontab" ;;
esac
''')
        self._w("dpkg-deb", 'echo "dpkg-deb $*" >> "' + C + '/calls.log"\n'
                            '[ "$1" = -x ] || exit 2\nmkdir -p "$3" && tar --force-local -xzf "$2" -C "$3"\n')
        self._w("ldd", 'case "$1" in *"${FAKE_LDD_MISSING:-@none@}") echo "libicu76.so => not found";; '
                       '*) echo "libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6";; esac\n')
        self._w("curl", 'while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; u="$1"; shift; done\n'
                        f'case "$out" in *server*) cp "{_posix(self.server_deb)}" "$out" ;; '
                        f'*) cp "{_posix(self.client_deb)}" "$out" ;; esac\n')
        self._w("http", f'''echo "http $*" >> "{C}/calls.log"
case "$1" in
  *:42014/health) [ -f "{C}/lm-active" ] && [ "${{FAKE_LM_UNHEALTHY:-0}}" = 0 ] ;;
  */health) [ -f "{C}/scratch-lm" ] ;;
  */api/campaigns*) [ -n "${{2:-}}" ] && [ -f "{C}/scratch-lm" ] || exit 1
                    echo '{{"data": {{"total": '"${{FAKE_API_TOTAL:-73}}"'}}}}' ;;
  *) exit 1 ;;
esac
''')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}/calls.log"
case "$1" in
  preflight) [ -n "${{FAKE_PROFILE-ultra}}" ] || exit 2; echo "${{FAKE_PROFILE-ultra}}" ;;
  in-window) exit "${{FAKE_INWINDOW_RC:-1}}" ;;
  task-ceiling) echo "${{FAKE_CEILING:-2000}}" ;;
esac
''')

    # --- run -----------------------------------------------------------------
    def run(self, *args, env=None, timeout=180):
        marker = self.tmp / "host.id"
        marker.write_text("test-slot\n")
        e = dict(os.environ,
                 HOME=_posix(self.home),
                 CALLS=_posix(self.calls),
                 FAKE_PROC=_posix(self.proc),
                 FAKE_UCC=_posix(self.ucc),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_APPS_DIR=_posix(self.apps),
                 QFLIX_UNIT_DIR=_posix(self.unitdir),
                 QFLIX_ENV_DIR=_posix(self.envdir),
                 QFLIX_SWAP_DIR=_posix(self.swap),
                 QFLIX_SECRETS_DIR=_posix(self.secrets),
                 MANITOBA_STATE_DIR=_posix(self.state),
                 QFLIX_MANIFEST=_posix(self.manifest),
                 QFLIX_PROC=_posix(self.proc),
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_APPCTL=_posix(self.stub / "appctl"),
                 QFLIX_SYSTEMCTL=_posix(self.stub / "systemctl"),
                 QFLIX_SS=_posix(self.stub / "ss"),
                 QFLIX_PS=_posix(self.stub / "ps"),
                 QFLIX_PGREP=_posix(self.stub / "pgrep"),
                 QFLIX_CRONTAB=_posix(self.stub / "crontab"),
                 QFLIX_DPKG_DEB=_posix(self.stub / "dpkg-deb"),
                 QFLIX_LDD=_posix(self.stub / "ldd"),
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_HTTP=_posix(self.stub / "http"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_PG_SERVER_SHA256=self.server_sha,
                 QFLIX_PG_CLIENT_SHA256=self.client_sha,
                 QFLIX_NOW=SATURDAY,
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.2",
                 QFLIX_STOP_TIMEOUT_S="3", QFLIX_PROOF_TIMEOUT_S="10")
        e.update(env or {})
        return subprocess.run(["bash", _posix(INSTALLER), *args], env=e,
                              capture_output=True, text=True, timeout=timeout)

    def ok(self, *args, env=None):
        r = self.run(*args, "--execute", env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def installed(self):
        return self.ok("--install")

    def proved(self):
        self.installed()
        return self.ok("--prove")

    def reset_calls(self):
        (self.calls / "calls.log").unlink(missing_ok=True)

    def ready(self):
        """Installed + proved, with the call log cleared so swap assertions
        only see the swap."""
        self.proved()
        self.reset_calls()

    def swapped(self):
        self.ready()
        return self.ok("--swap")


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


def _no_password_leak(box: Box, *results):
    for r in results:
        assert PW not in r.stdout + r.stderr
    assert PW not in box.calls_text()          # never in any argv we recorded


# --- static -----------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_exact_build_and_both_sha256_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("POSTGRES_VERSION="))
    assert ver == VER and f'VERSION="{ver}"' in text
    assert 'SERVER_SHA256="4f8b42bd3202d24953996743afbc90b609d3337c90bb740568b52d06ebfdcb15"' in text
    assert 'CLIENT_SHA256="c36408bb62178bc9193c113da65e30fc6a5237648de5e9db1ea594214df9ae4b"' in text


def test_240_stages_and_deploys_the_installer_and_helper():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/configure/312-native-postgres-install.sh \\\n" in text
    assert "    scripts/maint/pg_native.py \\\n" in text
    assert ("~/scripts/configure/312-native-postgres-install.sh\n"
            "chmod +x ~/scripts/configure/312-native-postgres-install.sh") in text
    assert 'cp -f   "$STG"/scripts/maint/pg_native.py ~/scripts/maint/pg_native.py' in text


def test_installer_never_calls_the_panel_tool_or_puts_a_password_in_env():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-postgres" in l for l in code)
    assert not any("PGPASSWORD" in l for l in code)
    assert not any(".encoded.dat" in l for l in code)


def test_golden_unit_is_what_the_installer_renders(box):
    box.installed()
    staged = box.appdir / "native" / UNIT
    assert staged.read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    unit = GOLDEN_UNIT.read_text(encoding="utf-8")
    assert ("ExecStart=%h/.apps/pg-native/bin/current/usr/lib/postgresql/17/bin/postgres "
            "-D %h/.apps/pg-native/data") in unit
    assert "KillSignal=SIGINT" in unit and "TimeoutStopSec=120" in unit
    assert "TasksMax" not in unit


# --- inert by default ---------------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"],
                                  ["--finish"], ["--rollback"], ["--post-upgrade", "17.12-1"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    assert box.calls_text() == ""


def test_unknown_flag_is_usage_error(box):
    assert box.run("--frobnicate").returncode == 64


def test_monday_window_refuses_execute(box):
    r = box.run("--install", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode != 0 and "window" in r.stderr.lower()
    assert not (box.appdir / "bin").exists()


def test_missing_host_profile_fails_closed(box):
    r = box.run("--install", "--execute", env={"FAKE_PROFILE": ""})
    assert r.returncode != 0
    assert not (box.appdir / "bin").exists()


# --- step 1: pin + install ----------------------------------------------------------------

def test_install_lays_out_both_debs_env_and_stages_unit_without_enabling(box):
    box.installed()
    bindir = box.appdir / "bin" / VER / "usr" / "lib" / "postgresql" / "17" / "bin"
    for b in ("postgres", "initdb", "pg_ctl", "psql", "pg_dump", "pg_dumpall", "pg_restore"):
        assert (bindir / b).exists(), b
    if os.name != "nt":
        assert os.readlink(box.appdir / "bin" / "current") == VER
    env = (box.envdir / "pg-native.env").read_text().splitlines()
    assert "MALLOC_ARENA_MAX=2" in env and "LANG=C.UTF-8" in env
    assert not (box.unitdir / UNIT).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running() and box.listmonk_active()


def test_install_refuses_a_different_container_build(box):
    box.container_up(build="17.12-1.pgdg13+1")
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "parity" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_when_the_container_is_down(box):
    shutil.rmtree(box.proc / "9001")
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "PG_VERSION" in r.stderr


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_PG_CLIENT_SHA256": "0" * 64})
    assert r.returncode != 0 and "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_unresolved_libraries(box):
    r = box.run("--install", "--execute", env={"FAKE_LDD_MISSING": "pg_dump"})
    assert r.returncode != 0 and "not found" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_a_binary_reporting_another_version(box):
    r = box.run("--install", "--execute", env={"FAKE_PG_VER": "17.10"})
    assert r.returncode != 0 and "--version" in r.stderr


# --- step 2: proof -----------------------------------------------------------------------

def test_prove_restores_compares_sanitizes_reads_campaigns_and_cleans_up(box):
    r = box.proved()
    assert "PROOF OK" in r.stdout
    proof = json.loads((box.swap / "postgres" / "proof.json").read_text())
    assert proof["ok"] is True and proof["campaigns"] == 73 and proof["delta"] == 60
    assert not (box.apps / ".prove" / "postgres").exists()
    calls = box.calls_text()
    # the scratch listmonk read the API with the token dir, never SMTP-on data
    assert "/api/campaigns?per_page=1" in calls
    lm = (box.calls / "scratch-listmonk.toml").read_text()
    assert 'address = "127.0.0.1:' in lm and 'port = 42009' not in lm
    # the live app was never touched
    assert box.container_running() and box.listmonk_active()
    assert "appctl stop" not in calls and "stop listmonk.service" not in calls
    assert box.crontab() == CRONTAB_FIXTURE and not box.suppressed()
    assert not list(box.swap.rglob(".pgpass"))
    _no_password_leak(box, r)


def test_prove_refuses_when_the_source_changes_during_the_dump(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_UCC_WRITES": "1"})
    assert r.returncode != 0 and "changed during the dump" in r.stderr
    assert not (box.swap / "postgres" / "proof.json").exists()


def test_prove_refuses_when_restored_counts_differ(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_DUMP_LOSES_ROW": "1"})
    assert r.returncode != 0 and "public.subscribers: 12 != 11" in r.stderr
    assert not (box.swap / "postgres" / "proof.json").exists()
    assert not (box.calls / "scratch-lm").exists()     # listmonk never booted


def test_prove_never_boots_listmonk_on_an_unsanitized_copy(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_SANITIZE_LEFT": "1"})
    assert r.returncode != 0 and "sanitize left" in r.stderr
    assert not (box.calls / "scratch-lm").exists()


def test_prove_refuses_when_listmonk_reads_other_campaigns(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_API_TOTAL": "72"})
    assert r.returncode != 0 and "72 campaigns" in r.stderr


def test_prove_refuses_globals_without_the_role_password(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_GLOBALS_NOPASS": "1"})
    assert r.returncode != 0
    assert not (box.swap / "postgres" / "proof.json").exists()


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1350 + 60 = 1410 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1350"})
    assert r.returncode != 0 and "70%" in r.stderr
    assert not (box.swap / "postgres" / "proof.json").exists()
    assert not (box.apps / ".prove" / "postgres").exists()


# --- the A13 swap --------------------------------------------------------------------------

def test_swap_refuses_without_a_proof(box):
    box.installed()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "prove" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_unless_the_pending_swap_flip_is_deployed(box):
    box.ready()
    box.set_manifest(cls="ucc", dormant=False)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert box.container_running() and box.listmonk_active()


def test_swap_refuses_within_24h_of_the_newsletter(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"QFLIX_NOW": MONDAY})
    assert r.returncode != 0 and "newsletter" in r.stderr
    assert box.container_running() and box.listmonk_active() and not box.suppressed()


def test_swap_refuses_while_a_campaign_is_running(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_RUNNING": "1"})
    assert r.returncode != 0 and "running" in r.stderr
    assert box.listmonk_active() and not box.suppressed()


def test_swap_refuses_a_wildcard_listen_set(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_UCC_LISTEN": "0.0.0.0"})
    assert r.returncode != 0 and "listen set" in r.stderr
    assert box.container_running() and box.listmonk_active() and not box.suppressed()


def test_swap_full_a13_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    order = ["crontab -", "systemctl --user stop listmonk.service", "pg_dumpall",
             "appctl stop postgres", "initdb", "pg_ctl-start listen=\n", "pg_restore",
             f"systemctl --user enable --now {UNIT}", "systemctl --user start listmonk.service"]
    idx = [calls.index(o) for o in order]
    assert idx == sorted(idx), list(zip(order, idx))
    # restore ran socket-only; the unit then binds exactly the recorded set
    assert (box.calls / "native-listen").read_text().strip() == f"127.0.0.1,172.17.0.1,{PUB}"
    assert (box.swap / "postgres" / "listen-set.before").read_text().split() == \
        sorted([f"127.0.0.1:42009", f"172.17.0.1:42009", f"{PUB}:42009"])
    # every database came across with its rows + sequences
    assert box.native_counts() == UCC_DBS["listmonk"][0]
    assert box.native_counts("jfstat") == UCC_DBS["jfstat"][0]
    before = json.loads((box.swap / "postgres" / "counts.before.json").read_text())
    assert before["listmonk"]["tables"]["public.subscribers"] == 12
    assert before == json.loads((box.swap / "postgres" / "counts.native.json").read_text())
    # the dumps are the snapshot and stay
    dumps = list((box.swap / "postgres").glob("dump-*"))
    assert len(dumps) == 1 and (dumps[0] / "listmonk.dump").exists() and (dumps[0] / "globals.sql").exists()
    assert not box.container_running() and box.native_running() and box.listmonk_active()
    assert (box.unitdir / UNIT).read_text() == GOLDEN_UNIT.read_text(encoding="utf-8")
    assert box.crontab() == CRONTAB_FIXTURE                    # hold released verbatim
    st = box.swapstate()
    assert st["ucc_version"] == VER and st["rollback_window"] == "open" and st["soak_until"]
    assert set(box.suppressed()) == {"postgres", "listmonk", "canary-thread-ceiling",
                                     "canary-cron-liveness"}
    assert "elapsed=" in r.stdout
    # the UCC data dir was never opened, so rollback restores nothing
    assert not any((box.apps / "postgres" / "data").iterdir())
    _no_password_leak(box, r)


def test_swap_holds_both_listmonk_writers_while_listmonk_is_down(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0
    assert (box.swap / "postgres" / "crontab.before").read_text() == CRONTAB_FIXTURE
    held = box.crontab().splitlines()
    assert held[0] == "#QFLX-37-HOLD# */5 * * * * /home/u/scripts/ops/heartbeat-listmonk.sh"
    assert held[1].startswith("#QFLX-37-HOLD# 0 4 * * * /usr/bin/python3")
    assert held[2] == "* * * * * /home/u/scripts/plex/stream_stats.sh"   # untouched
    assert not box.listmonk_active()


def test_swap_waits_out_a_running_sync_then_aborts_cleanly(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_SYNC_RUNNING": "1"})
    assert r.returncode != 0 and "listmonk-sync" in r.stderr
    assert box.listmonk_active() and box.container_running()
    assert box.crontab() == CRONTAB_FIXTURE and not box.suppressed()


def test_swap_aborts_when_something_still_writes(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_UCC_WRITES": "1"})
    assert r.returncode != 0 and "still writes" in r.stderr
    assert "appctl stop postgres" not in box.calls_text()
    assert box.container_running() and box.listmonk_active()
    assert box.crontab() == CRONTAB_FIXTURE and not box.suppressed()


def test_swap_aborts_and_restores_ucc_when_the_container_never_exits(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "initdb" not in box.calls_text() and "enable --now" not in box.calls_text()
    assert box.container_running() and box.listmonk_active() and not box.native_running()
    assert box.crontab() == CRONTAB_FIXTURE and not box.suppressed()


def test_swap_aborts_and_restores_ucc_when_restore_fails(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_RESTORE_FAILS": "1"})
    assert r.returncode != 0 and "restore failed" in r.stderr
    calls = box.calls_text()
    assert "enable --now" not in calls
    assert calls.rindex("appctl start postgres") > calls.index("appctl stop postgres")
    assert box.container_running() and box.listmonk_active() and not box.native_running()
    assert not (box.appdir / "run" / ".fake-data").exists()       # scratch boot stopped
    assert box.crontab() == CRONTAB_FIXTURE and not box.suppressed()


def test_swap_aborts_when_restored_counts_differ(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_DUMP_LOSES_ROW": "1"})
    assert r.returncode != 0 and "public.subscribers: 12 != 11" in r.stderr
    assert box.container_running() and box.listmonk_active() and not box.native_running()


def test_swap_parity_failure_after_cutover_keeps_suppression_and_hold(box):
    box.ready()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "postgres" in box.suppressed()
    assert "#QFLX-37-HOLD# " in box.crontab()


def test_swap_moves_a_leftover_cluster_aside_never_over_it(box):
    box.ready()
    (box.appdir / "data").mkdir(parents=True)
    (box.appdir / "data" / "PG_VERSION").write_text("17\n")
    box.ok("--swap")
    assert list(box.appdir.glob("data.aborted-*"))


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.ok("--swap")
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop postgres") == 1


def test_swap_repoints_listmonk_when_its_db_host_is_not_a_listener(box):
    box.ready()
    box.lmconf.write_text(LM_CONFIG.replace('host = "127.0.0.1"', 'host = "198.51.100.9"'), newline="\n")
    box.ok("--swap")
    assert 'host = "127.0.0.1"' in box.lmconf.read_text()
    assert (box.swap / "postgres" / "listmonk-db-host.orig").read_text().strip() == "198.51.100.9"


# --- finish ----------------------------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert "postgres" in box.suppressed()


def test_finish_lifts_app_and_canaries_together(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    box.ok("--finish")
    assert box.suppressed() == {}


# --- rollback ----------------------------------------------------------------------------------

def test_rollback_keeps_native_writes_masks_and_returns_to_ucc(box):
    box.swapped()
    r = box.ok("--rollback")
    calls = box.calls_text()
    assert calls.index(f"systemctl --user mask {UNIT}") < calls.rindex(f"systemctl --user stop {UNIT}")
    kept = list((box.swap / "postgres").glob("rollback-*"))
    assert len(kept) == 1 and (kept[0] / "listmonk.dump").exists()
    assert _masked(box.unitdir / UNIT)
    assert not box.native_running() and box.container_running() and box.listmonk_active()
    assert box.crontab() == CRONTAB_FIXTURE and box.suppressed() == {}
    assert "elapsed=" in r.stdout


def test_rollback_pauses_before_ucc_start_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10 and "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert not box.listmonk_active() and "#QFLX-37-HOLD# " in box.crontab()
    assert "postgres" in box.suppressed()
    box.set_manifest(swap_state="pending-swap")
    box.ok("--rollback")
    assert box.container_running() and box.listmonk_active()
    assert box.crontab() == CRONTAB_FIXTURE


def test_rollback_restores_the_listmonk_db_host(box):
    box.ready()
    box.lmconf.write_text(LM_CONFIG.replace('host = "127.0.0.1"', 'host = "198.51.100.9"'), newline="\n")
    box.ok("--swap")
    box.ok("--rollback")
    assert 'host = "198.51.100.9"' in box.lmconf.read_text()


def test_drill_rollback_then_reswap_unmasks(box):
    box.swapped()
    box.ok("--rollback")
    r = box.ok("--swap")
    assert f"systemctl --user unmask {UNIT}" in box.calls_text()
    assert not _masked(box.unitdir / UNIT)
    assert box.native_running() and not box.container_running() and box.listmonk_active()
    assert list(box.appdir.glob("data.aborted-*"))           # the first cluster kept
    _no_password_leak(box, r)


def test_rollback_with_nothing_swapped_is_harmless(box):
    box.installed()
    box.ok("--rollback")
    assert box.container_running() and box.listmonk_active()
    assert box.crontab() == CRONTAB_FIXTURE and box.suppressed() == {}


# --- post-upgrade (tarball_swap .deb post step) ----------------------------------------------

def _new_build(box: Box, ver: str, pgver: str):
    d = box.appdir / "bin" / ver / "usr" / "lib" / "postgresql" / "17" / "bin"
    d.mkdir(parents=True)
    for name, body in SERVER_BINS.items():
        (d / name).write_text(body.replace("${FAKE_PG_VER:-17.11}", pgver), newline="\n")
        (d / name).chmod(0o755)
    (box.appdir / "data").mkdir(exist_ok=True)
    (box.appdir / "data" / "PG_VERSION").write_text("17\n")
    return d


@pytest.mark.skipif(os.name == "nt", reason="Git Bash copies `current` instead of "
                    "linking, so the second atomic link swap hits a real dir")
def test_post_upgrade_minor_flips_current_and_carries_the_client(box):
    box.installed()
    d = _new_build(box, "17.12-1.pgdg13+1", "17.12")
    # inside the Monday sweep: the window gate must not refuse it
    r = box.ok("--post-upgrade", "17.12-1.pgdg13+1", env={"FAKE_INWINDOW_RC": "0"})
    assert "current flipped" in r.stdout
    assert (d / "psql").exists() and (d / "pg_dump").exists()
    if os.name != "nt":
        assert os.readlink(box.appdir / "bin" / "current") == "17.12-1.pgdg13+1"


def test_post_upgrade_refuses_a_major(box):
    box.installed()
    _new_build(box, "18.0-1.pgdg13+1", "18.0")
    r = box.run("--post-upgrade", "18.0-1.pgdg13+1", "--execute")
    assert r.returncode != 0
    assert "major" in r.stderr

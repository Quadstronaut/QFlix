"""scripts/configure/307-native-sonarr-install.sh (QFLX-32, A8, spec 5.9).

Subprocess tests in the shape of test_native_unpackerr_install.py (the pilot).
Every step runs against fakes that MODEL the box: a fake /proc tree (the UCC
container's pid lives in a docker cgroup, the native pid in the
qflix-sonarr.service cgroup), a fake `ss` whose answer depends on which runtime
is up, and fake appctl / systemctl / fuser / curl / hostpolicy that mutate that
tree the way the real tools would. "The container exited", "the unit is active"
and "the API answers the pinned build" are STATE the installer has to observe,
never an exit status it can trust.

What is specific to Sonarr (the shared .NET arr shape of prowlarr, QFLX-28):
  * a REAL sqlite sonarr.db is VACUUM-INTO'd, sanitized and read back by the
    fake binary AT BOOT, so "sanitized before boot" is observed, not assumed;
  * the panel prints "4.0.20", the API prints "4.0.20.3014": parity is prefix +
    API equality;
  * the proof compares the series count with the live app's (ticket QFLX-32);
  * PlexMetadata is enabled on the box and writes .plexmatch into the real
    series folders, so the proof copy turns every metadata consumer off;
  * the sonarr2 container runs the IDENTICAL cmdline: the fake box keeps it
    running the whole time and only /proc/<pid>/mountinfo tells them apart;
  * ucc.probe_app must not name sonarr (QFLX-17 prerequisite);
  * Kestrel binds one address: 172.17.0.1 natively + a systemd socket on
    loopback; the public-IP listener is a recorded D-4 exception.

Real python runs swapstate.py and suppression.py, so swap state and
push-suppress.json are the real files.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "307-native-sonarr-install.sh"
UNITS = REPO / "scripts" / "maint" / "systemd"
GOLDEN = {
    "qflix-sonarr.service": UNITS / "qflix-sonarr.service",
    "qflix-sonarr-fwd.socket": UNITS / "qflix-sonarr-fwd.socket",
    "qflix-sonarr-fwd.service": UNITS / "qflix-sonarr-fwd.service",
}
UNIT = "qflix-sonarr.service"
SOCKET = "qflix-sonarr-fwd.socket"
FWD = "qflix-sonarr-fwd.service"
VERSION = "4.0.20.3014"
PANEL_VERSION = "4.0.20"
PUBLIC = "192.0.2.7:17026"            # TEST-NET-1: stands in for the public-IP listener

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


def _masked(p: Path) -> bool:
    """systemd: linked to /dev/null OR an empty file, asked through bash."""
    f = _posix(p)
    return subprocess.run(["bash", "-c", f'[ -L "{f}" ] || {{ [ -f "{f}" ] && [ ! -s "{f}" ]; }}'],
                          capture_output=True).returncode == 0


def _uid() -> str:
    return subprocess.run(["bash", "-c", "id -u"], capture_output=True,
                          text=True).stdout.strip()


# The fake Sonarr apphost. It reads its sanitized database AT BOOT (so the test
# can see what state it was started on), records the environment the real unit
# would pass, registers 40 threads under the fake /proc and then idles.
FAKE_SONARR = r'''#!/usr/bin/env bash
data=""
for a in "$@"; do case "$a" in -data=*) data="${a#-data=}" ;; esac; done
[ -n "$data" ] || exit 2
# threads first: the installer counts them the moment the status answers
mkdir -p "$QFLIX_PROC/$$/task"
for i in $(seq 1 40); do mkdir -p "$QFLIX_PROC/$$/task/$i"; done
REC_ARGS="$*" REC_TZ="$TZ" "$QFLIX_PYTHON" - "$data" <<'PY'
import json, os, sqlite3, sys
d = sys.argv[1]
con = sqlite3.connect(os.path.join(d, "sonarr.db"))
def one(q):
    try:
        return con.execute(q).fetchone()[0]
    except sqlite3.Error:
        return -1
rec = {
    "import_lists_on": one('select count(*) from "ImportLists" where "EnableAutomaticAdd"!=0'),
    "clients_on": one('select count(*) from "DownloadClients" where "Enable"!=0'),
    "indexers_on": one('select count(*) from "Indexers" where "EnableRss"!=0 or "EnableAutomaticSearch"!=0 or "EnableInteractiveSearch"!=0'),
    "metadata_on": one('select count(*) from "Metadata" where "Enable"!=0'),
    "notifications": one('select count(*) from "Notifications"'),
    "series": one('select count(*) from "Series"'),
    "update_auto_off": "<UpdateAutomatically>False</UpdateAutomatically>"
                       in open(os.path.join(d, "config.xml"), encoding="utf-8").read(),
    "env": {k: v for k, v in os.environ.items()
            if k.startswith(("SONARR__", "DOTNET_", "COMPlus_", "MALLOC_"))},
    "tz": os.environ.get("REC_TZ"),
    "argv": os.environ.get("REC_ARGS", "").split(),
}
open(os.path.join(d, "boot.json"), "w").write(json.dumps(rec))
open(os.path.join(d, "series.json"), "w").write(json.dumps([{"id": i} for i in range(max(rec["series"], 0))]))
PY
while :; do sleep 0.2; done
'''


class Box:
    """A fake slot. Paths are POSIX strings for bash."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "sonarr"
        self.unitdir = self.home / ".config" / "systemd" / "user"
        self.envdir = self.home / ".config" / "qflix"
        self.secrets = self.home / "secrets"
        self.state = self.home / ".opt" / "maint"
        self.swap = self.state / "swap"
        self.proc = tmp / "proc"
        self.stub = tmp / "stub"
        self.calls = tmp / "calls.log"
        self.manifest = self.state / "apps.yaml"
        for d in (self.appdir, self.unitdir, self.envdir, self.secrets, self.swap,
                  self.proc, self.stub, self.home / "scripts" / "maint"):
            d.mkdir(parents=True, exist_ok=True)
        (self.secrets / "sonarr.port").write_text("17026\n")
        (self.secrets / "sonarr.key").write_text("k" * 32 + "\n")
        (self.secrets / "sonarr.urlbase").write_text("sonarr\n")
        (self.secrets / "net.app_host").write_text("172.17.0.1\n")
        (self.secrets / "ucc.probe_app").write_text("plex\n")
        self.write_config()
        self._db()
        self.set_manifest(swap_state="pending-swap")
        self.container_up()
        self._tarball()
        self._stubs()

    # --- fixtures ------------------------------------------------------------
    def write_config(self, *, auth="Enabled", key="k" * 32, extra=""):
        (self.appdir / "config.xml").write_text(
            "<Config>\n  <BindAddress>*</BindAddress>\n  <Port>8989</Port>\n"
            f"  <ApiKey>{key}</ApiKey>\n  <AuthenticationMethod>Forms</AuthenticationMethod>\n"
            f"  <AuthenticationRequired>{auth}</AuthenticationRequired>\n"
            "  <UrlBase>/sonarr</UrlBase>\n  <UpdateMechanism>Docker</UpdateMechanism>\n"
            f"{extra}</Config>", newline="\n")

    def _db(self, *, flag_cols=True):
        db = self.appdir / "sonarr.db"
        con = sqlite3.connect(db)
        # ImportLists without any known flag column: the sanitizer cannot prove it inert.
        il = ('"Id" INTEGER PRIMARY KEY, "Name" TEXT, "EnableAutomaticAdd" INTEGER' if flag_cols
              else '"Id" INTEGER PRIMARY KEY, "Name" TEXT')
        con.execute(f'CREATE TABLE "ImportLists" ({il})')
        for i, n in enumerate(("PlexImport", "TraktPopularImport"), 1):
            if flag_cols:
                con.execute('INSERT INTO "ImportLists" VALUES (?,?,1)', (i, n))
            else:
                con.execute('INSERT INTO "ImportLists" VALUES (?,?)', (i, n))
        con.execute('CREATE TABLE "DownloadClients" ("Id" INTEGER PRIMARY KEY, "Enable" INTEGER)')
        con.execute('INSERT INTO "DownloadClients" VALUES (1, 1), (2, 1)')
        con.execute('CREATE TABLE "Notifications" ("Id" INTEGER PRIMARY KEY, "Name" TEXT)')
        con.executemany('INSERT INTO "Notifications" VALUES (?,?)', [(1, "discord"), (2, "plex")])
        con.execute('CREATE TABLE "Indexers" ("Id" INTEGER PRIMARY KEY, "Name" TEXT, "EnableRss" INTEGER, '
                    '"EnableAutomaticSearch" INTEGER, "EnableInteractiveSearch" INTEGER, "Settings" TEXT)')
        con.execute('INSERT INTO "Indexers" VALUES (1, "nzbgeek", 1, 1, 1, \'{"baseUrl": "https://nzb.example/"}\')')
        con.execute('CREATE TABLE "Metadata" ("Id" INTEGER PRIMARY KEY, "Enable" INTEGER, "Name" TEXT)')
        con.executemany('INSERT INTO "Metadata" VALUES (?,?,?)',
                        [(1, 0, "Kodi"), (4, 1, "PlexMetadata"), (5, 0, "Kometa")])
        con.execute('CREATE TABLE "Series" ("Id" INTEGER PRIMARY KEY, "Title" TEXT, "Path" TEXT)')
        con.executemany('INSERT INTO "Series" VALUES (?,?,?)',
                        [(i, f"show {i}", f"/home/q/media/TV Shows/show {i}") for i in (1, 2, 3)])
        con.commit()
        con.close()

    def db_sha(self) -> str:
        return hashlib.sha256((self.appdir / "sonarr.db").read_bytes()).hexdigest()

    # --- state ---------------------------------------------------------------
    def _proc(self, pid: int, cgroup: str, cmd: str, mount: str | None = None):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{_uid()}\t{_uid()}\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)
        if mount:
            (d / "mountinfo").write_text(
                f"35997 35989 65:225 /quadstronaut/.apps/{mount} /config rw,nosuid - ext4 /dev/sdae1 rw\n"
                "36001 35989 0:50 / /proc rw - proc proc rw\n", newline="\n")

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope",
                   "/app/sonarr/bin/Sonarr -nobrowser -data=/config", mount="sonarr")
        self.sonarr2_up()

    def sonarr2_up(self):
        """sonarr2's container: same cmdline, same s6 name, different mount. It
        runs through every scenario and must never be mistaken for sonarr's."""
        self._proc(9003, "0::/system.slice/docker-def.scope",
                   "/app/sonarr/bin/Sonarr -nobrowser -data=/config", mount="sonarr2")

    def sonarr2_running(self) -> bool:
        return (self.proc / "9003").exists()

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  sonarr:", f"    class: {cls}", "    ucc_slug: sonarr"]
        if cls == "systemd":
            lines.append(f"    unit: {UNIT}")
        if dormant:
            lines.append("    ucc_dormant: true")
        if swap_state:
            lines.append(f"    swap_state: {swap_state}")
        self.manifest.write_text("\n".join(lines) + "\n", newline="\n")

    def calls_text(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""

    # --- fakes ---------------------------------------------------------------
    def _tarball(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            def add(name, data, mode=0o644):
                ti = tarfile.TarInfo(name)
                ti.size, ti.mode = len(data), mode
                tf.addfile(ti, io.BytesIO(data))
            top = tarfile.TarInfo("Sonarr")
            top.type, top.mode = tarfile.DIRTYPE, 0o755
            tf.addfile(top)
            add("Sonarr/Sonarr", FAKE_SONARR.encode(), 0o644)   # exec bit is the installer's job
            add("Sonarr/Sonarr.dll", b"MZ")
            add("Sonarr/Sonarr.Update/Sonarr.Update.dll", b"MZ")
        self.payload = self.tmp / "payload.tgz"
        self.payload.write_bytes(buf.getvalue())
        self.sha = hashlib.sha256(buf.getvalue()).hexdigest()

    def _w(self, name: str, body: str):
        p = self.stub / name
        p.write_text("#!/usr/bin/env bash\n" + body, newline="\n")
        p.chmod(0o755)

    def _stubs(self):
        P, C = _posix(self.proc), _posix(self.calls)
        U = _posix(self.unitdir)
        self._w("appctl", f'''echo "appctl $*" >> "{C}"
case "$1" in
  version) echo '{{"data": {{"version": "'"${{FAKE_UCC_VERSION:-4.0.20}}"'"}}, "result": true}}' ;;
  ports-free) echo "${{FAKE_PROOF_PORT:-34567}}"; echo 34568 ;;
  stop) [ "${{FAKE_CONTAINER_STICKS:-0}}" = 1 ] || rm -rf "{P}/9001" ;;
  start) [ "${{FAKE_START_NOOP:-0}}" = 1 ] && exit 0
         mkdir -p "{P}/9001/task/1"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
         printf '1 2 3:4 /quadstronaut/.apps/sonarr /config rw - ext4 x rw\\n' > "{P}/9001/mountinfo"
         printf '/app/sonarr/bin/Sonarr\\0-nobrowser\\0-data=/config\\0' > "{P}/9001/cmdline" ;;
  is-native) v="${{FAKE_ISNATIVE:-ucc}}"; echo "$v"; [ "$v" = native ] ;;
esac
''')
        self._w("systemctl", f'''echo "systemctl $*" >> "{C}"
[ "$1" = --user ] && shift
U="{U}"
case "$1" in
  is-active) [ -d "{P}/9002" ] && echo active && exit 0; echo inactive; exit 3 ;;
  enable) if [ "$2" = --now ]; then
            {{ [ -L "$U/$3" ] || [ ! -s "$U/$3" ]; }} && {{ echo "unit $3 is masked or missing" >&2; exit 1; }}
            [ "${{FAKE_NATIVE_FAILS:-0}}" = 1 ] && exit 0
            if [ "$3" = qflix-sonarr.service ]; then
              mkdir -p "{P}/9002/task/1"
              printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9002/status"
              echo "0::/user.slice/user-1.slice/app.slice/$3" > "{P}/9002/cgroup"
              printf '/h/.apps/sonarr/bin/current/Sonarr\\0-nobrowser\\0-data=/h/.apps/sonarr\\0' > "{P}/9002/cmdline"
            fi
          fi ;;
  stop) [ "$2" = qflix-sonarr.service ] && rm -rf "{P}/9002" ;;
  mask) [ -s "$U/$2" ] && [ ! -L "$U/$2" ] && {{ echo "Failed to mask unit: File $U/$2 already exists." >&2; exit 1; }}
        ln -sf /dev/null "$U/$2" ;;
  unmask) {{ [ -L "$U/$2" ] || [ ! -s "$U/$2" ]; }} && rm -f "$U/$2" ;;
esac
exit 0
''')
        # The listen set depends on WHICH runtime is up, exactly like the box.
        before = _posix(self.tmp / "ss-before.txt")
        after = _posix(self.tmp / "ss-after.txt")
        (self.tmp / "ss-before.txt").write_text(
            f"LISTEN 0 65535 169.150.251.170:17026 0.0.0.0:*\n".replace("169.150.251.170:17026", PUBLIC)
            + "LISTEN 0 65535 172.17.0.1:17026 0.0.0.0:*\nLISTEN 0 65535 127.0.0.1:17026 0.0.0.0:*\n")
        (self.tmp / "ss-after.txt").write_text(
            "LISTEN 0 65535 172.17.0.1:17026 0.0.0.0:*\nLISTEN 0 65535 127.0.0.1:17026 0.0.0.0:*\n")
        self._w("ss", f'''if [ -d "{P}/9001" ]; then cat "${{FAKE_SS_BEFORE:-{before}}}"
elif [ -d "{P}/9002" ]; then cat "${{FAKE_SS_AFTER:-{after}}}"; fi
exit 0
''')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("fuser", f'[ "${{FAKE_DB_BUSY:-0}}" = 1 ] && exit 0\n[ -d "{P}/9001" ] && exit 0\nexit 1\n')
        proof = _posix(self.apps / ".prove" / "sonarr" / "boot.json")
        self._w("curl", f'''out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; -H|--max-time|--retry) shift ;; http*) url="$1" ;; esac
  shift
done
if [ -n "$out" ]; then cp "{_posix(self.payload)}" "$out"; exit 0; fi
port="${{url#http://127.0.0.1:}}"; port="${{port%%/*}}"
ver="${{FAKE_API_VERSION:-{VERSION}}}"
if [ "$port" = 17026 ]; then
  if [ -d "{P}/9002" ]; then docker=false; ver="${{FAKE_API_VERSION_NATIVE:-$ver}}"
  elif [ -d "{P}/9001" ]; then docker=true
  else exit 7; fi
else
  [ -f "{proof}" ] || exit 7
  docker=false; ver="${{FAKE_API_VERSION_PROOF:-$ver}}"
fi
case "$url" in
  */api/v3/system/status) echo "{{\\"version\\": \\"$ver\\", \\"isDocker\\": $docker}}" ;;
  */api/v3/series)
    if [ "$port" = 17026 ]; then
      n="${{FAKE_LIVE_SERIES:-3}}"; printf '['; for i in $(seq 1 "$n"); do [ "$i" -gt 1 ] && printf ','; printf '{{"id": %s}}' "$i"; done; printf ']\\n'
    else cat "{_posix(self.apps / ".prove" / "sonarr" / "series.json")}"; fi ;;
  *) exit 22 ;;
esac
''')
        self._w("hostpolicy", f'''echo "hostpolicy $*" >> "{C}"
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
        # the installer finds native_sanitize.py at <HERE>/maint (HERE = scripts/)
        e = dict(os.environ,
                 HOME=_posix(self.home),
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
                 QFLIX_CURL=_posix(self.stub / "curl"),
                 QFLIX_FUSER=_posix(self.stub / "fuser"),
                 QFLIX_HOSTPOLICY=_posix(self.stub / "hostpolicy"),
                 QFLIX_SONARR_SHA256=self.sha,
                 QFLIX_POLL_S="0.2", QFLIX_SETTLE_S="0.3",
                 QFLIX_STOP_TIMEOUT_S="3", QFLIX_PROOF_TIMEOUT_S="20", QFLIX_API_TIMEOUT_S="3")
        e.update(env or {})
        return subprocess.run(["bash", _posix(INSTALLER), *args], env=e,
                              capture_output=True, text=True, timeout=timeout)

    def installed(self):
        r = self.run("--install", "--execute")
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def proved(self, **kw):
        self.installed()
        r = self.run("--prove", "--execute", **kw)
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def swapped(self):
        self.proved()
        r = self.run("--swap", "--execute")
        assert r.returncode == 0, r.stdout + r.stderr
        return r

    def suppressed(self) -> dict:
        p = self.state / "push-suppress.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def swapstate(self) -> dict:
        p = self.swap / "sonarr" / "state.json"
        return json.loads(p.read_text()) if p.exists() else {}


@pytest.fixture()
def box(tmp_path):
    return Box(tmp_path)


# --- static -------------------------------------------------------------------

def test_bash_syntax():
    r = subprocess.run(["bash", "-n", str(INSTALLER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_pins_exact_version_and_sha256_matching_versions_env():
    text = INSTALLER.read_text(encoding="utf-8")
    ver = next(l.split("=", 1)[1].strip() for l in
               (REPO / "versions.env").read_text(encoding="utf-8").splitlines()
               if l.startswith("SONARR_VERSION="))
    assert f'VERSION="{ver}"' in text
    assert 'SHA256="1fc48544b5a3401b2fc3df8d0642c1e6a73a28e41a351169edd9cd8f54770db5"' in text
    # the full four-part build, not the panel's truncated three
    assert ver.count(".") == 3


def test_240_stages_and_deploys_the_installer_with_its_deps():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/lib/native.sh \\\n" in text
    assert "    scripts/configure/307-native-sonarr-install.sh \\\n" in text
    assert "    scripts/maint/native_sanitize.py \\\n" in text
    assert 'cp -f   "$STG"/scripts/maint/native_sanitize.py ~/scripts/maint/native_sanitize.py' in text
    assert ("~/scripts/configure/307-native-sonarr-install.sh\n"
            "chmod +x ~/scripts/configure/307-native-sonarr-install.sh") in text


def test_installer_never_calls_the_panel_tool_directly():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-sonarr" in l for l in code)


def test_installer_is_executable_in_git():
    out = subprocess.run(["git", "ls-files", "-s", "scripts/configure/307-native-sonarr-install.sh"],
                         cwd=REPO, capture_output=True, text=True).stdout
    assert out.startswith("100755") or out == ""      # "" = not yet added


def test_golden_units_are_what_the_installer_renders(box):
    box.installed()
    for name, golden in GOLDEN.items():
        assert (box.appdir / "native" / name).read_text() == golden.read_text(encoding="utf-8"), name
    unit = GOLDEN[UNIT].read_text(encoding="utf-8")
    assert "ExecStart=%h/.apps/sonarr/bin/current/Sonarr -nobrowser -data=%h/.apps/sonarr" in unit
    assert "Environment=PATH=%h/.apps/sonarr/bin/current:" in unit
    assert "TasksMax" not in unit
    sock = GOLDEN[SOCKET].read_text(encoding="utf-8")
    assert "ListenStream=127.0.0.1:17026" in sock and "0.0.0.0" not in sock
    assert "systemd-socket-proxyd 172.17.0.1:17026" in GOLDEN[FWD].read_text(encoding="utf-8")


# --- inert by default -----------------------------------------------------------

@pytest.mark.parametrize("args", [[], ["--install"], ["--prove"], ["--swap"],
                                  ["--finish"], ["--rollback"]])
def test_without_execute_nothing_is_touched(box, args):
    before = sorted(p.as_posix() for p in box.tmp.rglob("*"))
    r = box.run(*args)
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout
    after = sorted(p.as_posix() for p in box.tmp.rglob("*") if p.name != "host.id")
    assert after == before
    assert "appctl" not in box.calls_text() and "systemctl" not in box.calls_text()


def test_unknown_flag_is_usage_error(box):
    assert box.run("--frobnicate").returncode == 64


def test_monday_window_refuses_execute(box):
    r = box.run("--install", "--execute", env={"FAKE_INWINDOW_RC": "0"})
    assert r.returncode != 0
    assert "window" in r.stderr.lower()
    assert not (box.appdir / "bin").exists()


def test_missing_host_profile_fails_closed(box):
    r = box.run("--install", "--execute", env={"FAKE_PROFILE": ""})
    assert r.returncode != 0
    assert not (box.appdir / "bin").exists()


@pytest.mark.parametrize("mode", ["swap", "finish", "rollback"])
def test_swap_modes_refuse_a_generic_host(box, mode):
    r = box.run(f"--{mode}", "--execute", env={"FAKE_PROFILE": "generic"})
    assert r.returncode != 0 and "generic" in r.stderr
    assert box.container_running()


# --- step 1: pin + install --------------------------------------------------------

def test_install_lays_out_binary_env_and_stages_units_without_enabling(box):
    box.installed()
    cur = box.appdir / "bin" / "current"
    # --strip-components: the apphost sits directly in bin/<ver>
    assert (box.appdir / "bin" / VERSION / "Sonarr").exists()
    assert (box.appdir / "bin" / VERSION / "Sonarr.dll").exists()
    assert not (box.appdir / "bin" / VERSION / "Sonarr.Update").exists()
    if os.name != "nt":   # Git Bash on Windows copies instead of linking
        assert os.readlink(cur) == VERSION
        assert os.access(cur / "Sonarr", os.X_OK)
    assert (cur / "Sonarr").exists()
    env = (box.envdir / "sonarr.env").read_text().splitlines()
    for line in ("DOTNET_PROCESSOR_COUNT=4", "DOTNET_gcServer=0", "MALLOC_ARENA_MAX=2",
                 "TZ=Europe/Amsterdam", "COMPlus_EnableDiagnostics=0",
                 "SONARR__SERVER__BINDADDRESS=172.17.0.1", "SONARR__SERVER__PORT=17026",
                 "SONARR__UPDATE__MECHANISM=External", "SONARR__UPDATE__AUTOMATICALLY=false"):
        assert line in env, line
    assert (box.envdir / "sonarr.env").stat().st_mode & 0o077 == 0 or os.name == "nt"
    for name in GOLDEN:
        assert (box.appdir / "native" / name).exists()
        # NOT in the unit dir and never enabled: WantedBy=default.target would
        # start it beside the live container (I-6).
        assert not (box.unitdir / name).exists()
    assert "enable" not in box.calls_text()
    assert box.container_running()
    # config.xml is used in place and left exactly as the container reads it
    assert "<Port>8989</Port>" in (box.appdir / "config.xml").read_text()
    assert "<UpdateMechanism>Docker</UpdateMechanism>" in (box.appdir / "config.xml").read_text()


def test_install_refuses_panel_version_that_is_not_a_prefix(box):
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "4.0.19"})
    assert r.returncode != 0 and "parity" in r.stderr
    assert not (box.appdir / "bin" / VERSION).exists()


def test_install_refuses_a_false_prefix(box):
    # "4.0.200" must not pass for 4.0.20.3014 (dotted-prefix, not string-prefix)
    r = box.run("--install", "--execute", env={"FAKE_UCC_VERSION": "4.0.200"})
    assert r.returncode != 0
    assert not (box.appdir / "bin" / VERSION).exists()


def test_install_refuses_when_the_api_build_differs(box):
    r = box.run("--install", "--execute", env={"FAKE_API_VERSION": "4.0.20.3010"})
    assert r.returncode != 0 and "API build" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_SONARR_SHA256": "0" * 64})
    assert r.returncode != 0
    assert "sha256" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_missing_bridge_secret_fails_closed(box):
    (box.secrets / "net.app_host").unlink()
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "net.app_host" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_installer_has_no_gateway_literal_on_an_executable_line():
    """C-12 (QFLX-23): the bridge comes from the net.app_host secret."""
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("172.17" + ".0.1" in l for l in code)


def test_install_refuses_a_port_secret_that_is_not_the_pinned_port(box):
    (box.secrets / "sonarr.port").write_text("17999\n")
    r = box.run("--install", "--execute")
    assert r.returncode != 0 and "17999" in r.stderr


@pytest.mark.skipif(os.name == "nt", reason="Git Bash copies `current` instead of "
                    "linking, so the second atomic link swap hits a real dir")
def test_install_is_idempotent(box):
    box.installed()
    box.installed()
    assert (box.appdir / "bin" / "current" / "Sonarr").exists()


# --- step 2: inert proof -----------------------------------------------------------

def test_prove_sanitizes_before_boot_and_never_touches_live_data(box):
    box.installed()
    db_before = box.db_sha()
    cfg_before = (box.appdir / "config.xml").read_bytes()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    proof_dir = box.apps / ".prove" / "sonarr"
    boot = json.loads((proof_dir / "boot.json").read_text())
    # what the BINARY saw when it started: zero syncing apps, zero notifications
    assert boot["import_lists_on"] == 0 and boot["clients_on"] == 0 and boot["indexers_on"] == 0
    assert boot["notifications"] == 0
    # PlexMetadata is ON in the live db; the booted copy must have it OFF (.plexmatch)
    assert boot["metadata_on"] == 0
    assert boot["update_auto_off"] is True
    # settings come from the environment, loopback only, on the claimed port
    env = boot["env"]
    assert env["SONARR__SERVER__BINDADDRESS"] == "127.0.0.1"
    assert env["SONARR__SERVER__PORT"] == "34567"
    assert env["SONARR__UPDATE__MECHANISM"] == "External"
    assert env["DOTNET_PROCESSOR_COUNT"] == "4" and env["MALLOC_ARENA_MAX"] == "2"
    assert boot["tz"] == "Europe/Amsterdam"
    assert boot["argv"] == ["-nobrowser", f"-data={proof_dir.as_posix()}"]
    # rows kept (flags flipped) except notifications, which are deleted
    con = sqlite3.connect(proof_dir / "sonarr.db")
    assert con.execute('select count(*) from "ImportLists"').fetchone()[0] == 2
    assert con.execute('select count(*) from "Series"').fetchone()[0] == 3
    con.close()
    # the live data is byte-identical and the container never stopped
    assert box.db_sha() == db_before
    assert (box.appdir / "config.xml").read_bytes() == cfg_before
    con = sqlite3.connect(box.appdir / "sonarr.db")
    assert con.execute('select count(*) from "ImportLists" where "EnableAutomaticAdd"=1').fetchone()[0] == 2
    assert con.execute('select count(*) from "Metadata" where "Enable"=1').fetchone()[0] == 1
    assert con.execute('select count(*) from "Notifications"').fetchone()[0] == 2
    con.close()
    assert box.container_running()
    assert "stop" not in box.calls_text()


def test_prove_records_status_series_parity_and_task_delta_and_cleans_up(box):
    r = box.proved()
    assert "delta=40" in r.stdout and "3 series match" in r.stdout
    assert not (box.apps / ".prove" / "sonarr").exists()
    proof = json.loads((box.swap / "sonarr" / "proof.json").read_text())
    assert proof["delta"] == 40 and proof["ceiling"] == 2000 and proof["ok"] is True
    assert proof["version"] == VERSION and proof["series"] == 3
    assert box.container_running()                      # live app untouched


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1362 + 40 = 1402 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1362"})
    assert r.returncode != 0
    assert "70%" in r.stderr
    assert not (box.swap / "sonarr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sonarr").exists()


def test_prove_refuses_when_the_copy_cannot_be_proven_inert(box):
    # ImportLists without any flag column: the sanitizer cannot prove it inert,
    # so the binary must never be booted on that copy.
    (box.appdir / "sonarr.db").unlink()
    box._db(flag_cols=False)
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode != 0
    assert "not inert" in r.stderr and "flag columns" in r.stderr
    assert not (box.apps / ".prove" / "sonarr" / "boot.json").exists()
    assert not (box.swap / "sonarr" / "proof.json").exists()


def test_prove_refuses_a_proof_status_with_the_wrong_build(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_API_VERSION_PROOF": "4.0.20.3010"})
    assert r.returncode != 0 and "pinned" in r.stderr
    assert not (box.swap / "sonarr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sonarr").exists()


def test_prove_refuses_a_series_count_that_differs_from_the_live_app(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_LIVE_SERIES": "4"})
    assert r.returncode != 0 and "series count differs" in r.stderr
    assert not (box.swap / "sonarr" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sonarr").exists()


def test_prove_needs_a_free_proof_port(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_PROOF_PORT": "17026"})
    assert r.returncode != 0 and "proof port" in r.stderr


# --- steps 3-6: swap ------------------------------------------------------------------

def test_swap_refuses_without_a_proof(box):
    box.installed()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "prove" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_unless_the_pending_swap_flip_is_deployed(box):
    box.proved()
    box.set_manifest(cls="ucc", dormant=False)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert box.container_running()


def test_swap_full_sequence(box):
    r = box.swapped()
    calls = box.calls_text()
    # suppress -> stop container -> container gone -> enable --now unit -> socket
    assert calls.index("appctl stop sonarr") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert calls.index(f"enable --now {UNIT}") < calls.index(f"enable --now {SOCKET}")
    assert not box.container_running() and box.native_running()
    for name, golden in GOLDEN.items():
        assert (box.unitdir / name).read_text() == golden.read_text(encoding="utf-8"), name
    # listen set captured: all three addresses, the public one becomes a recorded exception
    rec = (box.swap / "sonarr" / "listen-set.before").read_text().split()
    assert sorted(rec) == sorted([PUBLIC, "172.17.0.1:17026", "127.0.0.1:17026"])
    st = box.swapstate()
    assert st["ucc_version"] == VERSION
    assert st["exceptions"] == [PUBLIC]
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    # suppression stays ON until --finish (the manifest still says pending-swap)
    assert set(box.suppressed()) == {"sonarr", "canary-arr-plex-parity",
                                     "canary-seerr-arr-parity", "canary-ucc-gate-stuck",
                                     "canary-thread-ceiling"}
    # sonarr2's container (identical cmdline) was neither waited on nor stopped
    assert box.sonarr2_running()
    assert "appctl stop sonarr2" not in box.calls_text()
    assert list((box.swap / "sonarr").glob("snapshot-*.tgz"))
    assert (box.swap / "sonarr" / "config.xml.pre-native").exists()
    assert "elapsed=" in r.stdout


def test_swap_leaves_config_xml_and_db_untouched(box):
    box.proved()
    cfg, db = (box.appdir / "config.xml").read_bytes(), box.db_sha()
    assert box.run("--swap", "--execute").returncode == 0
    assert (box.appdir / "config.xml").read_bytes() == cfg
    assert box.db_sha() == db


def test_swap_refuses_a_wildcard_listener(box):
    box.proved()
    ss = box.tmp / "ss.txt"
    ss.write_text("LISTEN 0 4096 0.0.0.0:17026 0.0.0.0:*\n"
                  "LISTEN 0 4096 172.17.0.1:17026 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:17026 0.0.0.0:*\n")
    r = box.run("--swap", "--execute", env={"FAKE_SS_BEFORE": _posix(ss)})
    assert r.returncode != 0 and "wildcard" in r.stderr
    assert box.container_running() and not box.suppressed()


@pytest.mark.parametrize("missing", ["127.0.0.1:17026", "172.17.0.1:17026"])
def test_swap_refuses_a_set_missing_a_required_address(box, missing):
    box.proved()
    keep = [l for l in ("172.17.0.1:17026", "127.0.0.1:17026") if l != missing]
    ss = box.tmp / "ss.txt"
    ss.write_text("".join(f"LISTEN 0 4096 {a} 0.0.0.0:*\n" for a in keep))
    r = box.run("--swap", "--execute", env={"FAKE_SS_BEFORE": _posix(ss)})
    assert r.returncode != 0 and "lacks" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_local_address_auth_bypass(box):
    box.proved()
    box.write_config(auth="DisabledForLocalAddresses")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "AuthenticationRequired" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_when_config_apikey_differs_from_the_secret(box):
    box.proved()
    box.write_config(key="z" * 32)
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "ApiKey" in r.stderr
    assert "z" * 32 not in r.stdout + r.stderr          # never echoed
    assert box.container_running()


def test_swap_refuses_when_urlbase_differs_from_the_secret(box):
    box.proved()
    (box.secrets / "sonarr.urlbase").write_text("other\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "UrlBase" in r.stderr
    assert box.container_running()


def test_swap_refuses_container_paths_in_config(box):
    box.proved()
    box.write_config(extra="  <LogPath>/config/logs</LogPath>\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr
    assert box.container_running()


def test_swap_refuses_a_media_path_under_a_container_mount(box):
    box.proved()
    con = sqlite3.connect(box.appdir / "sonarr.db")
    con.execute('UPDATE "Series" SET "Path"=\'/data/TV Shows/show 1\' WHERE "Id"=1')
    con.commit()
    con.close()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "Series.Path" in r.stderr
    assert box.container_running()


def test_swap_refuses_when_the_gate_probe_still_names_sonarr(box):
    box.proved()
    (box.secrets / "ucc.probe_app").write_text("sonarr\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "probe_app" in r.stderr
    assert box.container_running() and not box.suppressed()
    assert "appctl stop" not in box.calls_text()


def test_swap_refuses_when_the_probe_secret_is_unreadable(box):
    box.proved()
    (box.secrets / "ucc.probe_app").unlink()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "probe_app" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_refuses_container_paths_in_the_database(box):
    box.proved()
    con = sqlite3.connect(box.appdir / "sonarr.db")
    con.execute('UPDATE "Indexers" SET "Settings"=\'{"cookiePath": "/config/cookies.txt"}\'')
    con.commit()
    con.close()
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr and "Indexers.Settings" in r.stderr
    assert box.container_running()


def test_swap_db_path_audit_ignores_ordinary_urls(box):
    box.proved()
    con = sqlite3.connect(box.appdir / "sonarr.db")
    con.execute('UPDATE "Indexers" SET "Settings"=\'{"baseUrl": "https://x.example/config.php"}\'')
    con.commit()
    con.close()
    assert box.run("--swap", "--execute").returncode == 0


def test_a_running_sonarr2_container_is_not_sonarrs_container(box):
    """Same cmdline, same docker cgroup shape: only the mount differs. With
    sonarr's container gone the swap must proceed while sonarr2 keeps running."""
    box.swapped()
    assert box.sonarr2_running() and not box.container_running()


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0
    assert "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running()
    assert box.container_running()
    assert not box.suppressed()          # back to normal monitoring


def test_swap_aborts_while_something_still_holds_the_database(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_DB_BUSY": "1"})
    assert r.returncode != 0 and "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.suppressed()


def test_swap_parity_failure_is_reported_and_stays_suppressed(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_NATIVE_FAILS": "1"})
    assert r.returncode != 0
    assert "--rollback" in r.stderr
    assert "sonarr" in box.suppressed()


def test_swap_fails_parity_when_the_listen_set_drifts(box):
    box.proved()
    ss = box.tmp / "after.txt"
    ss.write_text("LISTEN 0 4096 172.17.0.1:17026 0.0.0.0:*\n")      # loopback socket missing
    r = box.run("--swap", "--execute", env={"FAKE_SS_AFTER": _posix(ss)})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "sonarr" in box.suppressed()


def test_swap_fails_parity_when_the_native_api_reports_the_wrong_build(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_API_VERSION_NATIVE": "4.0.20.3010"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "sonarr" in box.suppressed()


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop sonarr") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert "sonarr" in box.suppressed()


def test_finish_lifts_app_and_canaries_together(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--finish", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 0, r.stderr
    assert box.suppressed() == {}


# --- rollback (0-5) + drill ---------------------------------------------------------------

def test_rollback_step0_suppresses_and_masks_all_three_units_before_stopping(box):
    box.swapped()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    calls = box.calls_text()
    for u in (UNIT, SOCKET, FWD):
        assert calls.index(f"systemctl --user mask {u}") < calls.index(f"systemctl --user stop {UNIT}")
        assert _masked(box.unitdir / u), u
    # the loopback socket is stopped before the app (it would re-spawn the forwarder)
    assert calls.index(f"systemctl --user stop {SOCKET}") < calls.index(f"systemctl --user stop {UNIT}")
    assert not box.native_running() and box.container_running()
    assert calls.rindex("appctl start sonarr") > calls.index(f"systemctl --user stop {UNIT}")
    assert box.suppressed() == {}
    assert "elapsed=" in r.stdout


def test_rollback_pauses_before_ucc_start_until_manifest_reverted(box):
    box.swapped()
    box.set_manifest(cls="systemd", swap_state=None)
    r = box.run("--rollback", "--execute", env={"FAKE_ISNATIVE": "native"})
    assert r.returncode == 10
    assert "revert" in r.stderr
    assert not box.native_running() and not box.container_running()
    assert "appctl start" not in box.calls_text()
    assert "sonarr" in box.suppressed()               # stays muted while paused
    # operator reverts the deployed manifest, re-runs: resumes at step 3
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()


def test_rollback_waits_for_sonarrs_own_container_not_sonarr2s(box):
    box.swapped()
    assert box.sonarr2_running()                       # a pattern-only scan would say "up"
    r = box.run("--rollback", "--execute", env={"FAKE_START_NOOP": "1"})
    assert r.returncode != 0 and "did not come back" in r.stderr
    assert not box.container_running() and box.sonarr2_running()
    assert "sonarr" in box.suppressed()                # still muted: sonarr is not back


def test_rollback_restores_config_xml_if_the_native_era_changed_it(box):
    box.swapped()
    original = (box.swap / "sonarr" / "config.xml.pre-native").read_bytes()
    (box.appdir / "config.xml").write_text(
        (box.appdir / "config.xml").read_text().replace("<Port>8989</Port>", "<Port>17026</Port>"),
        newline="\n")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.appdir / "config.xml").read_bytes() == original
    assert "<Port>17026</Port>" in (box.swap / "sonarr" / "config.xml.native-era").read_text()


def test_drill_rollback_then_reswap_unmasks_everything(box):
    box.swapped()
    assert box.run("--rollback", "--execute").returncode == 0
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    for u in (UNIT, SOCKET, FWD):
        assert f"systemctl --user unmask {u}" in box.calls_text()
        assert not _masked(box.unitdir / u), u
    assert box.native_running() and not box.container_running()


def test_rollback_with_nothing_swapped_is_harmless(box):
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()


def test_proof_boot_reserves_the_gc_region_like_the_unit():
    """The proof boots the binary by hand, outside the env file: without
    DOTNET_GCRegionRange CoreCLR cannot reserve its GC range under the slot's
    address-space cap and the proof never answers (radarr proof, box 2026-10-10)."""
    text = INSTALLER.read_text(encoding="utf-8")
    boot = [ln for ln in text.splitlines()
            if ln.lstrip().startswith("env DOTNET_PROCESSOR_COUNT=")]
    assert boot and all("DOTNET_GCRegionRange=80000000" in ln for ln in boot), boot

"""scripts/configure/305-native-sonarr2-install.sh (QFLX-30, A6, spec 5.9).

Subprocess tests in the shape of test_native_unpackerr_install.py (the pilot).
Every step runs against fakes that MODEL the box: a fake /proc tree (the UCC
container's pid lives in a docker cgroup, the native pid in the
qflix-sonarr2.service cgroup), a fake `ss` whose answer depends on which runtime
is up, and fake appctl / systemctl / fuser / curl / hostpolicy that mutate that
tree the way the real tools would. "The container exited", "the unit is active"
and "the API answers the pinned build" are STATE the installer has to observe,
never an exit status it can trust.

What is specific to sonarr2 (the anime Sonarr):
  * the sonarr container runs the SAME cmdline (/app/sonarr/bin/Sonarr), so a
    TWIN process is alive in every test and must never be mistaken for the
    sonarr2 container: the container is found by what holds sonarr2's own db
    (fuser), the native unit by its cgroup;
  * a REAL sqlite sonarr.db is VACUUM-INTO'd, sanitized and read back by the
    fake binary AT BOOT, so "sanitized before boot" is observed, not assumed;
  * renameEpisodes must be true in the proof, before the swap and after it;
    the series count must be the same in the proof copy and after the swap;
  * the panel prints "4.0.20", the API prints "4.0.20.3014": parity is prefix +
    API equality;
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
INSTALLER = REPO / "scripts" / "configure" / "305-native-sonarr2-install.sh"
UNITS = REPO / "scripts" / "maint" / "systemd"
GOLDEN = {
    "qflix-sonarr2.service": UNITS / "qflix-sonarr2.service",
    "qflix-sonarr2-fwd.socket": UNITS / "qflix-sonarr2-fwd.socket",
    "qflix-sonarr2-fwd.service": UNITS / "qflix-sonarr2-fwd.service",
}
UNIT = "qflix-sonarr2.service"
SOCKET = "qflix-sonarr2-fwd.socket"
FWD = "qflix-sonarr2-fwd.service"
VERSION = "4.0.20.3014"
PUBLIC = "192.0.2.7:17003"            # TEST-NET-1: stands in for the public-IP listener

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
FAKE_PROWLARR = r'''#!/usr/bin/env bash
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
    "download_clients": one('select count(*) from "DownloadClients" where "Enable"!=0'),
    "indexers": one('select count(*) from "Indexers" where "EnableRss"!=0 or "EnableAutomaticSearch"!=0 or "EnableInteractiveSearch"!=0'),
    "import_lists": one('select count(*) from "ImportLists" where "EnableAutomaticAdd"!=0'),
    "metadata": one('select count(*) from "Metadata" where "Enable"!=0'),
    "series": one('select count(*) from "Series"'),
    "notifications": one('select count(*) from "Notifications"'),
    "update_auto_off": "<UpdateAutomatically>False</UpdateAutomatically>"
                       in open(os.path.join(d, "config.xml"), encoding="utf-8").read(),
    "env": {k: v for k, v in os.environ.items()
            if k.startswith(("SONARR__", "DOTNET_", "COMPlus_", "MALLOC_"))},
    "tz": os.environ.get("REC_TZ"),
    "argv": os.environ.get("REC_ARGS", "").split(),
}
open(os.path.join(d, "boot.json"), "w").write(json.dumps(rec))
PY
while :; do sleep 0.2; done
'''


class Box:
    """A fake slot. Paths are POSIX strings for bash."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.apps = self.home / ".apps"
        self.appdir = self.apps / "sonarr2"
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
        (self.secrets / "sonarr2.port").write_text("17003\n")
        (self.secrets / "sonarr2.key").write_text("k" * 32 + "\n")
        (self.secrets / "sonarr2.urlbase").write_text("sonarr2\n")
        (self.secrets / "net.app_host").write_text("172.17.0.1\n")
        self.write_config()
        self._db()
        (self.appdir / "logs.db").write_bytes(b"")
        (self.appdir / "sonarr2.db").write_bytes(b"")          # the 0-byte stray on the box
        self.twin_up()
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
            "  <UrlBase>/sonarr2</UrlBase>\n  <UpdateMechanism>Docker</UpdateMechanism>\n"
            f"{extra}</Config>", newline="\n")

    def _db(self, *, meta_enable=True):
        db = self.appdir / "sonarr.db"
        con = sqlite3.connect(db)
        con.execute('CREATE TABLE "Series" ("Id" INTEGER PRIMARY KEY, "Title" TEXT, "Path" TEXT)')
        for i in range(1, 6):
            con.execute('INSERT INTO "Series" VALUES (?,?,?)', (i, f"anime {i}", f"/home/q/media/anime/{i}"))
        con.execute('CREATE TABLE "NamingConfig" ("Id" INTEGER PRIMARY KEY, "RenameEpisodes" INTEGER)')
        con.execute('INSERT INTO "NamingConfig" VALUES (1, 1)')
        con.execute('CREATE TABLE "DownloadClients" ("Id" INTEGER PRIMARY KEY, "Enable" INTEGER, "Settings" TEXT)')
        con.execute('INSERT INTO "DownloadClients" VALUES (1, 1, \'{"host": "172.17.0.1"}\')')
        con.execute('CREATE TABLE "Notifications" ("Id" INTEGER PRIMARY KEY, "Name" TEXT)')
        con.executemany('INSERT INTO "Notifications" VALUES (?,?)', [(1, "plex"), (2, "discord")])
        con.execute('CREATE TABLE "Indexers" ("Id" INTEGER PRIMARY KEY, "Name" TEXT, "EnableRss" INTEGER, '
                    '"EnableAutomaticSearch" INTEGER, "EnableInteractiveSearch" INTEGER, "Settings" TEXT)')
        con.execute('INSERT INTO "Indexers" VALUES (1, "nyaa", 1, 1, 1, \'{"baseUrl": "https://nyaa.example/"}\')')
        con.execute('CREATE TABLE "ImportLists" ("Id" INTEGER PRIMARY KEY, "Name" TEXT, "EnableAutomaticAdd" INTEGER)')
        con.execute('INSERT INTO "ImportLists" VALUES (1, "mal", 1)')
        mcols = '"Id" INTEGER PRIMARY KEY, "Name" TEXT, "Enable" INTEGER' if meta_enable \
            else '"Id" INTEGER PRIMARY KEY, "Name" TEXT'
        con.execute(f'CREATE TABLE "Metadata" ({mcols})')
        if meta_enable:
            con.execute('INSERT INTO "Metadata" VALUES (1, "Kodi", 1)')
        else:
            con.execute('INSERT INTO "Metadata" VALUES (1, "Kodi")')
        # container-era paths belong in history-like tables and must not trip the audit
        con.execute('CREATE TABLE "History" ("Id" INTEGER PRIMARY KEY, "Data" TEXT)')
        con.execute('INSERT INTO "History" VALUES (1, \'{"importedPath": "/downloads/x.mkv"}\')')
        con.commit()
        con.close()

    def db_sha(self) -> str:
        return hashlib.sha256((self.appdir / "sonarr.db").read_bytes()).hexdigest()

    # --- state ---------------------------------------------------------------
    def _proc(self, pid: int, cgroup: str, cmd: str):
        d = self.proc / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        (d / "status").write_text(f"Name:\tx\nUid:\t{_uid()}\t{_uid()}\n", newline="\n")
        (d / "cgroup").write_text(cgroup + "\n", newline="\n")
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        (d / "task" / "1").mkdir(parents=True, exist_ok=True)

    def container_up(self):
        self._proc(9001, "0::/system.slice/docker-abc.scope", "/app/sonarr/bin/Sonarr -nobrowser -data=/config")

    def twin_up(self):
        """The PRIMARY sonarr container: identical cmdline, its own cgroup, never ours."""
        self._proc(9100, "0::/system.slice/docker-def.scope", "/app/sonarr/bin/Sonarr -nobrowser -data=/config")

    def container_running(self) -> bool:
        return (self.proc / "9001").exists()

    def native_running(self) -> bool:
        return (self.proc / "9002").exists()

    def set_manifest(self, *, cls="systemd", swap_state=None, dormant=True):
        lines = ["apps:", "  sonarr2:", f"    class: {cls}", "    ucc_slug: sonarr2"]
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
            add("Sonarr/Sonarr", FAKE_PROWLARR.encode(), 0o644)   # exec bit is the installer's job
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
  start) mkdir -p "{P}/9001/task/1"
         printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9001/status"
         echo "0::/system.slice/docker-abc.scope" > "{P}/9001/cgroup"
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
            if [ "$3" = qflix-sonarr2.service ]; then
              mkdir -p "{P}/9002/task/1"
              printf 'Name:\\tx\\nUid:\\t%s\\t%s\\n' "$(id -u)" "$(id -u)" > "{P}/9002/status"
              echo "0::/user.slice/user-1.slice/app.slice/$3" > "{P}/9002/cgroup"
              printf '/h/.apps/sonarr2/bin/current/Sonarr\\0-nobrowser\\0-data=/h/.apps/sonarr2\\0' > "{P}/9002/cmdline"
            fi
          fi ;;
  stop) [ "$2" = qflix-sonarr2.service ] && rm -rf "{P}/9002" ;;
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
            f"LISTEN 0 65535 169.150.251.170:17003 0.0.0.0:*\n".replace("169.150.251.170:17003", PUBLIC)
            + "LISTEN 0 65535 172.17.0.1:17003 0.0.0.0:*\nLISTEN 0 65535 127.0.0.1:17003 0.0.0.0:*\n")
        (self.tmp / "ss-after.txt").write_text(
            "LISTEN 0 65535 172.17.0.1:17003 0.0.0.0:*\nLISTEN 0 65535 127.0.0.1:17003 0.0.0.0:*\n")
        self._w("ss", f'''if [ -d "{P}/9001" ]; then cat "${{FAKE_SS_BEFORE:-{before}}}"
elif [ -d "{P}/9002" ]; then cat "${{FAKE_SS_AFTER:-{after}}}"; fi
exit 0
''')
        self._w("ps", 'n=${FAKE_TASKS:-1000}; for i in $(seq 1 "$n"); do echo x; done\n')
        self._w("fuser", f'''silent=0
[ "$1" = -s ] && {{ silent=1; shift; }}
held=1
if [ "${{FAKE_DB_BUSY:-0}}" = 1 ]; then held=0; [ "$silent" = 0 ] && echo " 9999"; fi
if [ -d "{P}/9001" ]; then held=0; [ "$silent" = 0 ] && echo " 9001"; fi
if [ "$silent" = 0 ] && [ -d "{P}/9002" ]; then held=0; echo " 9002"; fi
exit $held
''')
        proof = _posix(self.apps / ".prove" / "sonarr2" / "boot.json")
        self._w("curl", f'''out=""; url=""
while [ $# -gt 0 ]; do
  case "$1" in -o) out="$2"; shift ;; -H|--max-time|--retry) shift ;; http*) url="$1" ;; esac
  shift
done
if [ -n "$out" ]; then cp "{_posix(self.payload)}" "$out"; exit 0; fi
port="${{url#http://127.0.0.1:}}"; port="${{port%%/*}}"
ver="${{FAKE_API_VERSION:-{VERSION}}}"
rename=true; series="${{FAKE_SERIES:-5}}"
if [ "$port" = 17003 ]; then
  if [ -d "{P}/9002" ]; then docker=false; ver="${{FAKE_API_VERSION_NATIVE:-$ver}}"
    rename="${{FAKE_RENAME_NATIVE:-true}}"; series="${{FAKE_SERIES_NATIVE:-$series}}"
  elif [ -d "{P}/9001" ]; then docker=true; rename="${{FAKE_RENAME:-true}}"
  else exit 7; fi
else
  [ -f "{proof}" ] || exit 7
  docker=false; ver="${{FAKE_API_VERSION_PROOF:-$ver}}"
  rename="${{FAKE_RENAME_PROOF:-true}}"; series="${{FAKE_SERIES_PROOF:-$series}}"
fi
case "$url" in
  */api/v3/system/status) echo "{{\\"version\\": \\"$ver\\", \\"isDocker\\": $docker}}" ;;
  */api/v3/config/naming) echo "{{\\"renameEpisodes\\": $rename}}" ;;
  */api/v3/series) printf '['; for i in $(seq 1 "$series"); do [ "$i" -gt 1 ] && printf ','; printf '{{"id": %s}}' "$i"; done; printf ']' ;;
  */api/v3/queue*) echo "{{\\"records\\": [{{\\"trackedDownloadState\\": \\"${{FAKE_QUEUE_STATE:-downloading}}\\"}}]}}" ;;
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
                 QFLIX_SONARR2_SHA256=self.sha,
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
        p = self.swap / "sonarr2" / "state.json"
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
               if l.startswith("SONARR2_VERSION="))
    assert f'VERSION="{ver}"' in text
    assert 'SHA256="1fc48544b5a3401b2fc3df8d0642c1e6a73a28e41a351169edd9cd8f54770db5"' in text
    # the full four-part build, not the panel's truncated three
    assert ver.count(".") == 3


def test_240_stages_and_deploys_the_installer_with_its_deps():
    text = (REPO / "scripts" / "configure" / "240-maintenance-install.sh").read_text(encoding="utf-8")
    assert "    scripts/lib/native.sh \\\n" in text
    assert "    scripts/configure/305-native-sonarr2-install.sh \\\n" in text
    assert "    scripts/maint/native_sanitize.py \\\n" in text
    assert 'cp -f   "$STG"/scripts/maint/native_sanitize.py ~/scripts/maint/native_sanitize.py' in text
    assert ("~/scripts/configure/305-native-sonarr2-install.sh\n"
            "chmod +x ~/scripts/configure/305-native-sonarr2-install.sh") in text


def test_installer_never_calls_the_panel_tool_directly():
    code = [l for l in INSTALLER.read_text(encoding="utf-8").splitlines()
            if not l.strip().startswith("#")]
    assert not any("app-sonarr2" in l for l in code)


def test_installer_is_executable_in_git():
    out = subprocess.run(["git", "ls-files", "-s", "scripts/configure/305-native-sonarr2-install.sh"],
                         cwd=REPO, capture_output=True, text=True).stdout
    assert out.startswith("100755") or out == ""      # "" = not yet added


def test_golden_units_are_what_the_installer_renders(box):
    box.installed()
    for name, golden in GOLDEN.items():
        assert (box.appdir / "native" / name).read_text() == golden.read_text(encoding="utf-8"), name
    unit = GOLDEN[UNIT].read_text(encoding="utf-8")
    assert "ExecStart=%h/.apps/sonarr2/bin/current/Sonarr -nobrowser -data=%h/.apps/sonarr2" in unit
    assert "Environment=PATH=%h/.apps/sonarr2/bin/current:" in unit
    assert "TasksMax" not in unit
    sock = GOLDEN[SOCKET].read_text(encoding="utf-8")
    assert "ListenStream=127.0.0.1:17003" in sock and "0.0.0.0" not in sock
    assert "systemd-socket-proxyd 172.17.0.1:17003" in GOLDEN[FWD].read_text(encoding="utf-8")


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
    env = (box.envdir / "sonarr2.env").read_text().splitlines()
    for line in ("DOTNET_PROCESSOR_COUNT=4", "DOTNET_gcServer=0", "MALLOC_ARENA_MAX=2",
                 "TZ=Europe/Amsterdam", "COMPlus_EnableDiagnostics=0",
                 "SONARR__SERVER__BINDADDRESS=172.17.0.1", "SONARR__SERVER__PORT=17003",
                 "SONARR__UPDATE__MECHANISM=External", "SONARR__UPDATE__AUTOMATICALLY=false"):
        assert line in env, line
    assert (box.envdir / "sonarr2.env").stat().st_mode & 0o077 == 0 or os.name == "nt"
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
    r = box.run("--install", "--execute", env={"FAKE_API_VERSION": "4.0.20.3000"})
    assert r.returncode != 0 and "API build" in r.stderr
    assert not (box.appdir / "bin").exists()


def test_install_refuses_sha_mismatch(box):
    r = box.run("--install", "--execute", env={"QFLIX_SONARR2_SHA256": "0" * 64})
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
    (box.secrets / "sonarr2.port").write_text("17999\n")
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
    proof_dir = box.apps / ".prove" / "sonarr2"
    boot = json.loads((proof_dir / "boot.json").read_text())
    # what the BINARY saw when it started: zero syncing apps, zero notifications
    assert boot["download_clients"] == 0 and boot["indexers"] == 0
    assert boot["import_lists"] == 0 and boot["notifications"] == 0
    assert boot["metadata"] == 0                       # nfo/image writers off in the copy
    assert boot["series"] == 5                         # the library itself is intact
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
    assert con.execute('select count(*) from "Series"').fetchone()[0] == 5
    assert con.execute('select count(*) from "Indexers"').fetchone()[0] == 1
    con.close()
    # the live data is byte-identical and the container never stopped
    assert box.db_sha() == db_before
    assert (box.appdir / "config.xml").read_bytes() == cfg_before
    con = sqlite3.connect(box.appdir / "sonarr.db")
    assert con.execute('select count(*) from "Indexers" where "EnableRss"=1').fetchone()[0] == 1
    assert con.execute('select count(*) from "Metadata" where "Enable"=1').fetchone()[0] == 1
    assert con.execute('select count(*) from "Notifications"').fetchone()[0] == 2
    con.close()
    assert box.container_running()
    assert "stop" not in box.calls_text()


def test_prove_records_status_rename_series_and_task_delta_and_cleans_up(box):
    r = box.proved()
    assert "delta=40" in r.stdout and "renameEpisodes true" in r.stdout and "5 series" in r.stdout
    assert not (box.apps / ".prove" / "sonarr2").exists()
    proof = json.loads((box.swap / "sonarr2" / "proof.json").read_text())
    assert proof["delta"] == 40 and proof["ceiling"] == 2000 and proof["ok"] is True
    assert proof["version"] == VERSION and proof["series"] == 5 and proof["rename_episodes"] is True
    assert box.container_running()                      # live app untouched


def test_prove_refuses_at_seventy_percent_of_ceiling(box):
    box.installed()
    # 1362 + 40 = 1402 >= 0.70 * 2000
    r = box.run("--prove", "--execute", env={"FAKE_TASKS": "1362"})
    assert r.returncode != 0
    assert "70%" in r.stderr
    assert not (box.swap / "sonarr2" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sonarr2").exists()


def test_prove_refuses_when_the_copy_cannot_be_proven_inert(box):
    # Metadata without an Enable column: the copy cannot be proven inert, so
    # the binary must never be booted on it (it would write nfo files).
    (box.appdir / "sonarr.db").unlink()
    box._db(meta_enable=False)
    box.installed()
    r = box.run("--prove", "--execute", env={"QFLIX_KEEP_PROOF": "1"})
    assert r.returncode != 0
    assert "not booting" in r.stderr and "Enable" in r.stderr
    assert not (box.apps / ".prove" / "sonarr2" / "boot.json").exists()
    assert not (box.swap / "sonarr2" / "proof.json").exists()


def test_prove_refuses_a_proof_status_with_the_wrong_build(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_API_VERSION_PROOF": "4.0.20.3000"})
    assert r.returncode != 0 and "pinned" in r.stderr
    assert not (box.swap / "sonarr2" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sonarr2").exists()


def test_prove_needs_a_free_proof_port(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_PROOF_PORT": "17003"})
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
    assert calls.index("appctl stop sonarr2") < calls.index(f"systemctl --user enable --now {UNIT}")
    assert calls.index(f"enable --now {UNIT}") < calls.index(f"enable --now {SOCKET}")
    assert not box.container_running() and box.native_running()
    for name, golden in GOLDEN.items():
        assert (box.unitdir / name).read_text() == golden.read_text(encoding="utf-8"), name
    # listen set captured: all three addresses, the public one becomes a recorded exception
    rec = (box.swap / "sonarr2" / "listen-set.before").read_text().split()
    assert sorted(rec) == sorted([PUBLIC, "172.17.0.1:17003", "127.0.0.1:17003"])
    st = box.swapstate()
    assert st["ucc_version"] == VERSION
    assert st["exceptions"] == [PUBLIC]
    assert st["swap_date"] and st["soak_until"] and st["rollback_window"] == "open"
    # suppression stays ON until --finish (the manifest still says pending-swap)
    assert set(box.suppressed()) == {"sonarr2", "canary-anime", "canary-arr-plex-parity",
                                     "canary-seerr-arr-parity", "canary-thread-ceiling"}
    snaps = list((box.swap / "sonarr2").glob("snapshot-*.tgz"))
    assert snaps
    with tarfile.open(snaps[0]) as tf:
        names = tf.getnames()
    assert "sonarr2/sonarr.db" in names and "sonarr2/config.xml" in names
    assert not any(n.startswith("sonarr2/bin") or n.startswith("sonarr2/native") for n in names)
    assert (box.swap / "sonarr2" / "config.xml.pre-native").exists()
    assert (box.swap / "sonarr2" / "series.count").read_text().strip() == "5"
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
    ss.write_text("LISTEN 0 4096 0.0.0.0:17003 0.0.0.0:*\n"
                  "LISTEN 0 4096 172.17.0.1:17003 0.0.0.0:*\nLISTEN 0 4096 127.0.0.1:17003 0.0.0.0:*\n")
    r = box.run("--swap", "--execute", env={"FAKE_SS_BEFORE": _posix(ss)})
    assert r.returncode != 0 and "wildcard" in r.stderr
    assert box.container_running() and not box.suppressed()


@pytest.mark.parametrize("missing", ["127.0.0.1:17003", "172.17.0.1:17003"])
def test_swap_refuses_a_set_missing_a_required_address(box, missing):
    box.proved()
    keep = [l for l in ("172.17.0.1:17003", "127.0.0.1:17003") if l != missing]
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
    (box.secrets / "sonarr2.urlbase").write_text("other\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "UrlBase" in r.stderr
    assert box.container_running()


def test_swap_refuses_container_paths_in_config(box):
    box.proved()
    box.write_config(extra="  <LogPath>/config/logs</LogPath>\n")
    r = box.run("--swap", "--execute")
    assert r.returncode != 0 and "container path" in r.stderr
    assert box.container_running()


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


def test_swap_aborts_when_container_never_exits_and_restores_service(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_CONTAINER_STICKS": "1"})
    assert r.returncode != 0
    assert "did not exit" in r.stderr
    assert "enable --now" not in box.calls_text()
    assert not box.native_running()
    assert box.container_running()
    assert not box.suppressed()          # back to normal monitoring
    assert not list((box.swap / "sonarr2").glob("snapshot-*.tgz"))     # never tar a live WAL db


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
    assert "sonarr2" in box.suppressed()


def test_swap_fails_parity_when_the_listen_set_drifts(box):
    box.proved()
    ss = box.tmp / "after.txt"
    ss.write_text("LISTEN 0 4096 172.17.0.1:17003 0.0.0.0:*\n")      # loopback socket missing
    r = box.run("--swap", "--execute", env={"FAKE_SS_AFTER": _posix(ss)})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "sonarr2" in box.suppressed()


def test_swap_fails_parity_when_the_native_api_reports_the_wrong_build(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_API_VERSION_NATIVE": "4.0.20.3000"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "sonarr2" in box.suppressed()


def test_swap_is_resumable_when_already_swapped(box):
    box.swapped()
    r = box.run("--swap", "--execute")
    assert r.returncode == 0, r.stderr
    assert "already" in r.stdout
    assert box.calls_text().count("appctl stop sonarr2") == 1


# --- step 9: finish -----------------------------------------------------------------

def test_finish_refuses_while_manifest_still_pending_swap(box):
    box.swapped()
    r = box.run("--finish", "--execute")
    assert r.returncode != 0 and "pending-swap" in r.stderr
    assert "sonarr2" in box.suppressed()


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
    assert calls.rindex("appctl start sonarr2") > calls.index(f"systemctl --user stop {UNIT}")
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
    assert "sonarr2" in box.suppressed()               # stays muted while paused
    # operator reverts the deployed manifest, re-runs: resumes at step 3
    box.set_manifest(swap_state="pending-swap")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stderr
    assert box.container_running()


def test_rollback_restores_config_xml_if_the_native_era_changed_it(box):
    box.swapped()
    original = (box.swap / "sonarr2" / "config.xml.pre-native").read_bytes()
    (box.appdir / "config.xml").write_text(
        (box.appdir / "config.xml").read_text().replace("<Port>8989</Port>", "<Port>17003</Port>"),
        newline="\n")
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.appdir / "config.xml").read_bytes() == original
    assert "<Port>17003</Port>" in (box.swap / "sonarr2" / "config.xml.native-era").read_text()


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


# --- sonarr2-specific ---------------------------------------------------------------

def test_the_twin_sonarr_container_is_never_mistaken_for_sonarr2(box):
    """The primary sonarr container has the same cmdline and stays up throughout."""
    box.swapped()
    assert (box.proc / "9100").exists()              # twin untouched by the swap
    assert box.native_running() and not box.container_running()
    r = box.run("--rollback", "--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert (box.proc / "9100").exists()


def test_swap_refuses_without_fuser(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"QFLIX_FUSER": "/nonexistent/fuser"})
    assert r.returncode != 0 and "fuser" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_only_the_real_db_is_audited_and_snapshotted_not_the_stray(box):
    # the 0-byte sonarr2.db stray must neither break the audit nor be required
    box.proved()
    assert box.run("--swap", "--execute").returncode == 0


def test_swap_db_path_audit_ignores_history_tables(box):
    # History holds /downloads/... from the container era on purpose (fixture row)
    box.proved()
    assert box.run("--swap", "--execute").returncode == 0


def test_prove_refuses_rename_episodes_false_in_the_copy(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_RENAME_PROOF": "false"})
    assert r.returncode != 0 and "renameEpisodes" in r.stderr
    assert not (box.swap / "sonarr2" / "proof.json").exists()
    assert not (box.apps / ".prove" / "sonarr2").exists()


def test_prove_refuses_a_series_count_mismatch(box):
    box.installed()
    r = box.run("--prove", "--execute", env={"FAKE_SERIES_PROOF": "4"})
    assert r.returncode != 0 and "series count differs" in r.stderr
    assert not (box.swap / "sonarr2" / "proof.json").exists()


def test_swap_refuses_when_live_rename_episodes_is_false(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_RENAME": "false"})
    assert r.returncode != 0 and "renameEpisodes" in r.stderr
    assert box.container_running() and not box.suppressed()


@pytest.mark.parametrize("state", ["importing", "importPending"])
def test_swap_refuses_while_an_import_is_in_flight(box, state):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_QUEUE_STATE": state})
    assert r.returncode != 0 and "import" in r.stderr
    assert box.container_running() and not box.suppressed()


def test_swap_parity_fails_when_native_rename_episodes_is_false(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_RENAME_NATIVE": "false"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "renameEpisodes" in r.stderr
    assert "sonarr2" in box.suppressed()


def test_swap_parity_fails_when_the_native_series_count_differs(box):
    box.proved()
    r = box.run("--swap", "--execute", env={"FAKE_SERIES_NATIVE": "3"})
    assert r.returncode != 0 and "--rollback" in r.stderr
    assert "series count" in r.stderr
    assert "sonarr2" in box.suppressed()


def test_swap_refused_without_the_deployed_flip_leaves_no_suppression(box):
    box.proved()
    box.set_manifest(cls="ucc", dormant=False)
    assert box.run("--swap", "--execute").returncode != 0
    assert not box.suppressed()


def test_env_file_never_carries_the_prowlarr_prefix(box):
    box.installed()
    env = (box.envdir / "sonarr2.env").read_text()
    assert "PROWLARR__" not in env and "SONARR__SERVER__PORT=17003" in env


def test_finish_text_documents_the_buildarr_oneshot():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "buildarr.service" in text

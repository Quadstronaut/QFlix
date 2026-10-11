"""scripts/configure/92-sabnzbd-vhost.sh (QFLX-33): HTTPS ingress for SABnzbd.

Runs the real script in on-host mode (QFLIX_HOST_ID_FILE marker, so sshm/scpm_to
act locally) against a fake $HOME that models the slot: ~/.apps/nginx with the
panel's sites-enabled/default (listen 17040) and proxy.d/, ~/secrets with the
SAB port + key, a SAB ini. Fakes: nginx (-t verdict), appctl (records
proxy-reload), curl (SAB get/set_config against a whitelist state file, and the
nginx probe that answers 303 only once the fragment is installed, nginx was
reloaded and the Host is whitelisted -- the three things the real path needs).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "configure" / "92-sabnzbd-vhost.sh"
TMPL = REPO / "scripts" / "data" / "sabnzbd.nginx.conf.tmpl"
PORT = "17007"
KEY = "0123456789abcdef0123456789abcdef"
SLOT = "slot.seedbox.example.com"
WL = "seedbox.example.com,other.seedbox.example.com"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _posix(p) -> str:
    return Path(p).as_posix()


FAKE_CURL = r'''#!/usr/bin/env bash
url=""; host=""; w=0
while [ $# -gt 0 ]; do
  case "$1" in
    -H) host="${2#Host: }"; shift 2 ;;
    -w) w=1; shift 2 ;;
    -o|--max-time) shift 2 ;;
    -*) shift ;;
    *) url="$1"; shift ;;
  esac
done
echo "$url" >> "$FAKE_DIR/curl.log"
case "$url" in
  *"mode=get_config"*)
    "$QFLIX_PYTHON" -c 'import json,sys; print(json.dumps({"config":{"misc":{"host_whitelist":[t for t in open(sys.argv[1]).read().strip().split(",") if t]}}}))' "$FAKE_DIR/wl" ;;
  *"mode=set_config"*)
    [ -n "${FAKE_SET_IGNORED:-}" ] && { echo '{"status":true}'; exit 0; }
    "$QFLIX_PYTHON" -c 'import sys,urllib.parse as u; q=u.parse_qs(u.urlparse(sys.argv[1]).query); open(sys.argv[2],"w").write(",".join(s.strip() for s in q["value"][0].split(",")))' "$url" "$FAKE_DIR/wl"
    echo '{"status":true}' ;;
  http://127.0.0.1:17040/sabnzbd/)
    code=404
    if [ -f "$HOME/.apps/nginx/proxy.d/sabnzbd.conf" ] && [ -f "$FAKE_DIR/reloaded" ] \
       && grep -q "^$host\$" <(tr ',' '\n' < "$FAKE_DIR/wl"); then code=303; fi
    [ "$w" = 1 ] && printf '%s' "$code" ;;
  *) exit 7 ;;
esac
'''

FAKE_NGINX = r'''#!/usr/bin/env bash
echo "nginx $*" >> "$FAKE_DIR/nginx.log"
if [ -n "${FAKE_NGINX_FAIL:-}" ]; then
  echo 'nginx: [emerg] unexpected "}" in proxy.d/sabnzbd.conf:3'; exit 1
fi
echo 'nginx: the configuration file nginx.conf syntax is ok'
echo 'nginx: configuration file nginx.conf test is successful'
'''

FAKE_APPCTL = r'''#!/usr/bin/env bash
echo "appctl $*" >> "$FAKE_DIR/appctl.log"
touch "$FAKE_DIR/reloaded"
'''


class Box:
    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.ngx = self.home / ".apps" / "nginx"
        (self.ngx / "proxy.d").mkdir(parents=True)
        (self.ngx / "sites-enabled").mkdir()
        (self.ngx / "sites-enabled" / "default").write_text(
            "server {\n    listen       17040;\n    include proxy.d/*.conf;\n}\n")
        self.secrets = self.home / "secrets"
        self.secrets.mkdir()
        (self.secrets / "sabnzbd.port").write_text(PORT + "\n")
        (self.secrets / "sabnzbd.key").write_text(KEY + "\n")
        self.ini = self.home / ".apps" / "sabnzbd" / "sabnzbd.ini"
        self.ini.parent.mkdir(parents=True)
        self.ini.write_text("[misc]\nhost_whitelist = %s\n" % WL.replace(",", ", "))
        self.fake = tmp / "fake"
        self.fake.mkdir()
        (self.fake / "wl").write_text(WL)
        for name, body in (("curl", FAKE_CURL), ("nginx", FAKE_NGINX), ("appctl", FAKE_APPCTL)):
            p = self.fake / name
            p.write_text(body, newline="\n")
            p.chmod(0o755)
        self.frag = self.ngx / "proxy.d" / "sabnzbd.conf"

    def run(self, *args, env=None):
        marker = self.tmp / "host.id"
        marker.write_text("test-slot\n")
        e = dict(os.environ,
                 HOME=_posix(self.home),
                 FAKE_DIR=_posix(self.fake),
                 QFLIX_HOST_ID_FILE=_posix(marker),
                 QFLIX_PUBLIC_HOST=SLOT,
                 QFLIX_SKIP_PUBLIC_SMOKE="1",
                 QFLIX_PYTHON=_posix(sys.executable),
                 QFLIX_CURL=_posix(self.fake / "curl"),
                 QFLIX_NGINX_BIN=_posix(self.fake / "nginx"),
                 QFLIX_APPCTL=_posix(self.fake / "appctl"),
                 QFLIX_SETTLE_S="0")
        e.update(env or {})
        return subprocess.run(["bash", _posix(SCRIPT), *args], env=e,
                              capture_output=True, text=True, timeout=120)

    def log(self, name):
        p = self.fake / name
        return p.read_text() if p.exists() else ""

    def wl(self):
        return (self.fake / "wl").read_text().split(",")

    def staged_leftovers(self):
        return sorted(p.name for p in self.ngx.iterdir() if p.name.startswith(".qflix-sabnzbd"))


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_syntax():
    r = subprocess.run(["bash", "-n", _posix(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_template_has_one_token_and_a_loopback_upstream():
    t = TMPL.read_text()
    body = [l for l in t.splitlines() if not l.lstrip().startswith("#")]
    tokens = {w for l in body for w in l.replace(";", " ").split() if "@@" in w}
    assert tokens == {"http://127.0.0.1:@@PORT@@"}
    assert "location ^~ /sabnzbd {" in body
    # path passed through untouched (SAB url_base = /sabnzbd): no URI on proxy_pass
    assert any(l.strip() == "proxy_pass       http://127.0.0.1:@@PORT@@;" for l in body)
    assert "\r" not in t


def test_dry_run_changes_nothing(box):
    r = box.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fragment: missing" in r.stdout
    assert f"host_whitelist: MISSING {SLOT}" in r.stdout
    assert "DRY-RUN" in r.stdout
    assert not box.frag.exists()
    assert box.log("appctl.log") == "" and box.log("nginx.log") == ""
    assert SLOT not in box.wl()
    assert box.staged_leftovers() == []
    assert "set_config" not in box.log("curl.log")


def test_execute_installs_whitelists_reloads_and_proves(box):
    r = box.run("--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    frag = box.frag.read_text()
    assert f"proxy_pass       http://127.0.0.1:{PORT};" in frag and "@@" not in frag
    assert box.wl() == WL.split(",") + [SLOT]          # appended, nothing lost
    assert list(box.ini.parent.glob("sabnzbd.ini.bak.qflx33.*"))
    assert box.log("nginx.log").count("-t") == 1
    assert box.log("appctl.log").splitlines() == ["appctl proxy-reload"]
    assert f"PROOF nginx:17040 Host={SLOT} /sabnzbd/ -> 303" in r.stdout
    assert box.staged_leftovers() == []
    # backups never land in proxy.d where nginx would include them
    assert sorted(p.name for p in (box.ngx / "proxy.d").iterdir()) == ["sabnzbd.conf"]


def test_second_run_is_idempotent(box):
    assert box.run("--execute").returncode == 0
    r = box.run("--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "fragment: identical" in r.stdout
    assert box.log("appctl.log").splitlines() == ["appctl proxy-reload"]   # no 2nd reload
    assert box.wl().count(SLOT) == 1
    assert len(list(box.ini.parent.glob("sabnzbd.ini.bak.qflx33.*"))) == 1


def test_nginx_t_failure_restores_previous_fragment_and_never_reloads(box):
    box.frag.write_text("# previous fragment\n")
    r = box.run("--execute", env={"FAKE_NGINX_FAIL": "1"})
    assert r.returncode != 0
    assert "nginx -t rejected" in r.stderr
    assert box.frag.read_text() == "# previous fragment\n"
    assert box.log("appctl.log") == ""
    bak = list(box.ngx.glob("sabnzbd.conf.bak.qflx33.*"))
    assert len(bak) == 1 and bak[0].read_text() == "# previous fragment\n"


def test_nginx_t_failure_with_no_previous_fragment_removes_it(box):
    r = box.run("--execute", env={"FAKE_NGINX_FAIL": "1"})
    assert r.returncode != 0
    assert not box.frag.exists()
    assert box.log("appctl.log") == ""


def test_whitelist_set_that_does_not_stick_fails_before_nginx(box):
    r = box.run("--execute", env={"FAKE_SET_IGNORED": "1"})
    assert r.returncode != 0
    assert "still lacks" in r.stderr
    assert not box.frag.exists()
    assert box.log("nginx.log") == "" and box.log("appctl.log") == ""


def test_bad_port_secret_refuses(box):
    (box.secrets / "sabnzbd.port").write_text("17007; evil\n")
    r = box.run("--execute")
    assert r.returncode != 0
    assert "is not a port" in r.stderr
    assert not box.frag.exists()


def test_already_whitelisted_host_is_not_rewritten(box):
    (box.fake / "wl").write_text(WL + "," + SLOT)
    r = box.run("--execute")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "set_config" not in box.log("curl.log")
    assert not list(box.ini.parent.glob("sabnzbd.ini.bak.qflx33.*"))

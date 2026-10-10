"""340-native-uptimekuma-install.sh and 341-native-proxy-install.sh (QFLX-41).

Subprocess tests with fakes (curl/npm/systemctl/nginx/caddy) and HOME in tmp_path.
Nothing here touches the network, a real systemd, or the real secrets dir.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KUMA = REPO / "scripts" / "configure" / "340-native-uptimekuma-install.sh"
PROXY = REPO / "scripts" / "configure" / "341-native-proxy-install.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _p(path) -> str:
    """Path as bash sees it: MSYS /c/... on Windows (GNU tar reads `C:` as a host)."""
    s = Path(path).as_posix()
    if os.name == "nt" and len(s) > 1 and s[1] == ":":
        s = "/" + s[0].lower() + s[2:]
    return s


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _tar(path: Path, files: dict, mode: str, top: str = "") -> Path:
    with tarfile.open(path, mode) as t:
        for name, (data, perm) in files.items():
            ti = tarfile.TarInfo((top + "/" if top else "") + name)
            ti.size = len(data)
            ti.mode = perm
            t.addfile(ti, io.BytesIO(data))
    return path


def _exe(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, newline="\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    sec = tmp_path / "secrets"
    sec.mkdir()
    (sec / "host.profile").write_text("generic\n")
    stub = tmp_path / "stub"
    stub.mkdir()
    pay = tmp_path / "pay"
    pay.mkdir()
    # --- payloads -------------------------------------------------------------
    node = _tar(pay / "node.tar.xz", {"bin/node": (b"#!/bin/sh\n", 0o755),
                                      "bin/npm": (b"#!/bin/sh\n", 0o755)}, "w:xz", "node-v1")
    kuma = _tar(pay / "kuma.tar.gz", {"server/server.js": (b"//", 0o644),
                                      "package.json": (b"{}", 0o644)}, "w:gz", "uptime-kuma-1")
    dist = _tar(pay / "dist.tar.gz", {"dist/index.html": (b"<html>", 0o644)}, "w:gz")
    caddy = _tar(pay / "caddy.tar.gz",
                 {"caddy": (b"#!/bin/sh\necho caddy-called \"$@\" >> \"$CADDY_LOG\"\nexit 0\n", 0o755)},
                 "w:gz")
    # fake curl: map the URL's basename-ish key to a payload, honouring -o
    _exe(stub / "curl",
         'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; http*) url="$1";; esac; shift; done\n'
         'case "$url" in\n'
         f'  *node-v*) cp "{node.as_posix()}" "$out";;\n'
         f'  */archive/*) cp "{kuma.as_posix()}" "$out";;\n'
         f'  */dist.tar.gz) cp "{dist.as_posix()}" "$out";;\n'
         f'  *caddy_*) cp "{caddy.as_posix()}" "$out";;\n'
         '  *) exit 22;;\n'
         'esac\n')
    _exe(stub / "npm", 'echo "npm $@" >> "$NPM_LOG"; mkdir -p node_modules\n')
    _exe(stub / "systemctl", 'echo "systemctl $@" >> "$SC_LOG"\n'
                              '[ "$2" = is-active ] && exit ${FAKE_ACTIVE:-3}\nexit 0\n')
    _exe(stub / "nginx", 'echo "nginx $@" >> "$NGINX_LOG"\nexit ${FAKE_NGINX_RC:-0}\n')
    start = tmp_path / "port_start"
    start.write_text("1024")
    env = dict(
        os.environ, HOME=_p(home), QFLIX_APPS_DIR=_p(home / ".apps"),
        MANITOBA_SECRETS_DIR=_p(sec), QFLIX_PYTHON=Path(sys.executable).as_posix(),
        QFLIX_CURL=_p(stub / "curl"), QFLIX_NPM=_p(stub / "npm"),
        QFLIX_SYSTEMCTL=_p(stub / "systemctl"), QFLIX_NGINX=_p(stub / "nginx"),
        QFLIX_ARCH="x86_64", QFLIX_PORT_START_FILE=_p(start),
        QFLIX_NODE_SHA_X64=_sha(node), QFLIX_KUMA_SHA=_sha(kuma),
        QFLIX_KUMA_DIST_SHA=_sha(dist), QFLIX_CADDY_SHA_X64=_sha(caddy),
        NPM_LOG=_p(tmp_path / "npm.log"), SC_LOG=_p(tmp_path / "sc.log"),
        NGINX_LOG=_p(tmp_path / "nginx.log"), CADDY_LOG=_p(tmp_path / "caddy.log"),
        PATH=_p(stub) + os.pathsep + os.environ["PATH"])
    env.pop("XDG_CONFIG_HOME", None)
    return {"tmp": tmp_path, "home": home, "sec": sec, "env": env, "stub": stub}


def _run(script, box, *args, env_extra=None):
    env = dict(box["env"], **(env_extra or {}))
    return subprocess.run(["bash", str(script), *args], env=env, capture_output=True,
                          text=True, cwd=box["tmp"])


def _tree(home):
    return sorted(str(p.relative_to(home)) for p in home.rglob("*"))


def test_bash_syntax():
    for s in (KUMA, PROXY):
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


# --- hostpolicy gate (both installers) ------------------------------------------

@pytest.mark.parametrize("script,args", [(KUMA, ["--port", "42050"]),
                                         (PROXY, ["--flavor", "nginx", "--domain", "a.example.org"])])
@pytest.mark.parametrize("profile", ["ultra", "bogus", None])
def test_refuses_non_generic_even_dry_run_and_execute(box, script, args, profile):
    if profile is None:
        (box["sec"] / "host.profile").unlink()
    else:
        (box["sec"] / "host.profile").write_text(profile)
    for extra in ([], ["--execute"]):
        r = _run(script, box, *args, *extra)
        assert r.returncode == 2, r.stderr
        assert _tree(box["home"]) == []


def test_refuses_ultra_with_panel_tool_present(box):
    (box["sec"] / "host.profile").write_text("ultra")
    _exe(box["stub"] / "app-ports", "exit 0\n")      # detect() true -> preflight prints ultra
    r = _run(KUMA, box, "--port", "42050", "--execute")
    # POSIX: preflight prints "ultra" -> "generic-host installer". Windows python
    # cannot see the extensionless stub -> "unresolved". Either way: refused, exit 2.
    assert r.returncode == 2 and ("generic" in r.stderr or "unresolved" in r.stderr)
    assert _tree(box["home"]) == []


# --- kuma -------------------------------------------------------------------------

def test_kuma_dry_run_is_inert(box):
    r = _run(KUMA, box, "--port", "42050")
    assert r.returncode == 0, r.stderr
    assert "PLAN" in r.stdout and "dry run" in r.stderr
    assert _tree(box["home"]) == []
    assert not (box["tmp"] / "sc.log").exists()


def test_kuma_needs_port(box):
    assert _run(KUMA, box).returncode != 0


def test_kuma_port_from_secret_and_install(box):
    (box["sec"] / "uptimekuma.port").write_text("42050\n")
    r = _run(KUMA, box, "--execute")
    assert r.returncode == 0, r.stderr
    h = box["home"]
    base = h / ".apps" / "uptimekuma"
    assert (base / "bin" / "1.23.16" / "app" / "server" / "server.js").exists()
    assert (base / "bin" / "1.23.16" / "app" / "dist" / "index.html").exists()
    assert (base / "bin" / "current" / "app" / "server" / "server.js").exists()
    if os.name == "posix":   # MSYS bash on Windows copies instead of linking
        assert (base / "bin" / "current").is_symlink()
    assert (base / "data").is_dir()
    unit = (h / ".config/systemd/user/qflix-uptimekuma.service").read_text()
    assert "--disable-wasm-trap-handler" in unit and "NODE_OPTIONS" not in unit
    assert "bin/current/app/server/server.js" in unit and "TasksMax" not in unit
    env = (h / ".config/qflix/uptimekuma.env").read_text()
    assert "UPTIME_KUMA_PORT=42050" in env and "UPTIME_KUMA_HOST=127.0.0.1" in env
    assert "NODE_OPTIONS" not in env
    assert "npm ci --omit=dev" in (box["tmp"] / "npm.log").read_text()
    sc = (box["tmp"] / "sc.log").read_text()
    assert "daemon-reload" in sc and "enable qflix-uptimekuma.service" in sc
    assert " start" not in sc                       # enable only, never start


def test_kuma_sha_mismatch_aborts_before_install(box):
    r = _run(KUMA, box, "--port", "42050", "--execute",
             env_extra={"QFLIX_KUMA_SHA": "0" * 64})
    assert r.returncode != 0 and "sha256 mismatch" in r.stderr
    assert not (box["home"] / ".config").exists()
    assert not (box["home"] / ".apps" / "uptimekuma" / "bin" / "current").exists()


def _kuma_db(path: Path):
    import sqlite3
    con = sqlite3.connect(path)
    con.execute("create table monitor(name text, push_token text)")
    con.execute("insert into monitor values('x','tok')")
    con.commit()
    con.close()
    return path


def test_kuma_import_db_preserves_tokens(box):
    src = _kuma_db(box["tmp"] / "kuma-copy.db")
    r = _run(KUMA, box, "--port", "42050", "--execute", "--import-db", _p(src))
    assert r.returncode == 0, r.stderr
    dst = box["home"] / ".apps/uptimekuma/data/kuma.db"
    assert dst.read_bytes() == src.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(dst.stat().st_mode) == 0o600


def test_kuma_import_refuses_existing_db(box):
    src = _kuma_db(box["tmp"] / "kuma-copy.db")
    data = box["home"] / ".apps/uptimekuma/data"
    data.mkdir(parents=True)
    (data / "kuma.db").write_bytes(b"live")
    r = _run(KUMA, box, "--port", "42050", "--execute", "--import-db", _p(src))
    assert r.returncode != 0 and "already exists" in r.stderr
    assert (data / "kuma.db").read_bytes() == b"live"


def test_kuma_import_refuses_active_unit(box):
    src = _kuma_db(box["tmp"] / "kuma-copy.db")
    r = _run(KUMA, box, "--port", "42050", "--execute", "--import-db", _p(src),
             env_extra={"FAKE_ACTIVE": "0"})
    assert r.returncode != 0 and "active" in r.stderr
    assert not (box["home"] / ".apps/uptimekuma/data/kuma.db").exists()


def test_kuma_import_refuses_corrupt_db(box):
    bad = box["tmp"] / "bad.db"
    bad.write_bytes(b"not sqlite at all" * 50)
    r = _run(KUMA, box, "--port", "42050", "--execute", "--import-db", _p(bad))
    assert r.returncode != 0 and "integrity_check" in r.stderr


# --- proxy ------------------------------------------------------------------------

def _proxy_secrets(box):
    import yaml
    data = yaml.safe_load((REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8"))
    for i, (n, a) in enumerate((data["apps"]).items()):
        h = (a or {}).get("health") or {}
        if h.get("port_secret"):
            (box["sec"] / h["port_secret"]).write_text(str(42100 + i))
        if h.get("urlbase_secret"):
            (box["sec"] / h["urlbase_secret"]).write_text("/" + n)


def test_proxy_requires_flavor_and_domain(box):
    _proxy_secrets(box)
    assert _run(PROXY, box, "--domain", "a.example.org").returncode != 0
    assert _run(PROXY, box, "--flavor", "caddy").returncode != 0
    assert _run(PROXY, box, "--flavor", "apache", "--domain", "a.example.org").returncode != 0


def test_proxy_dry_run_is_inert(box):
    _proxy_secrets(box)
    r = _run(PROXY, box, "--flavor", "caddy", "--domain", "a.example.org")
    assert r.returncode == 0, r.stderr
    assert "PLAN" in r.stdout and _tree(box["home"]) == []
    assert "unprivileged ports" in r.stderr           # 1024 > 80 warning


def test_proxy_missing_port_secret_aborts_clean(box):
    _proxy_secrets(box)
    (box["sec"] / "seerr.port").unlink()
    r = _run(PROXY, box, "--flavor", "caddy", "--domain", "a.example.org", "--execute")
    assert r.returncode != 0 and "render failed" in r.stderr
    assert _tree(box["home"]) == []


def test_proxy_caddy_install(box):
    _proxy_secrets(box)
    r = _run(PROXY, box, "--flavor", "caddy", "--domain", "a.example.org",
             "--email", "ops@example.org", "--execute")
    assert r.returncode == 0, r.stderr
    h = box["home"]
    cf = (h / ".config/qflix/proxy/Caddyfile").read_text()
    assert "seerr.a.example.org {" in cf and "email ops@example.org" in cf
    assert (h / ".apps/proxy/bin/current/caddy").exists()
    unit = (h / ".config/systemd/user/qflix-proxy.service").read_text()
    assert "bin/current/caddy run --config %h/.config/qflix/proxy/Caddyfile" in unit
    assert "GOMAXPROCS" in (h / ".config/qflix/proxy.env").read_text()
    assert "validate" in (box["tmp"] / "caddy.log").read_text()
    sc = (box["tmp"] / "sc.log").read_text()
    assert "enable qflix-proxy.service" in sc and " start" not in sc


def test_proxy_caddy_sha_mismatch(box):
    _proxy_secrets(box)
    r = _run(PROXY, box, "--flavor", "caddy", "--domain", "a.example.org", "--execute",
             env_extra={"QFLIX_CADDY_SHA_X64": "f" * 64})
    assert r.returncode != 0 and "sha256 mismatch" in r.stderr
    assert not (box["home"] / ".config/systemd").exists()


def test_proxy_caddy_arm64_unpinned_refused(box):
    _proxy_secrets(box)
    r = _run(PROXY, box, "--flavor", "caddy", "--domain", "a.example.org", "--execute",
             env_extra={"QFLIX_ARCH": "aarch64"})
    assert r.returncode != 0 and "no pinned sha256" in r.stderr


def test_proxy_nginx_install(box):
    _proxy_secrets(box)
    r = _run(PROXY, box, "--flavor", "nginx", "--domain", "a.example.org", "--execute")
    assert r.returncode == 0, r.stderr
    h = box["home"]
    conf = (h / ".config/qflix/proxy/qflix.conf").read_text()
    assert "@@CERTDIR@@" not in conf and _p(h / ".config/qflix/proxy/certs") in conf
    assert "server_name seerr.a.example.org;" in conf
    main = (h / ".config/qflix/proxy/nginx.conf").read_text()
    assert f"include {_p(h)}/.config/qflix/proxy/qflix.conf;" in main
    assert "-t -c" in (box["tmp"] / "nginx.log").read_text()
    unit = (h / ".config/systemd/user/qflix-proxy.service").read_text()
    assert "bin/current/nginx -c %h/.config/qflix/proxy/nginx.conf -g 'daemon off;'" in unit


def test_proxy_nginx_bad_config_not_enabled(box):
    _proxy_secrets(box)
    r = _run(PROXY, box, "--flavor", "nginx", "--domain", "a.example.org", "--execute",
             env_extra={"FAKE_NGINX_RC": "1"})
    assert r.returncode != 0 and "nginx -t failed" in r.stderr
    assert not (box["home"] / ".config/systemd").exists()

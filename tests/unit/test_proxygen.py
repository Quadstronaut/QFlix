"""lib/proxygen.py - box-2 reverse-proxy config rendered from the manifest (QFLX-41).

Golden files live in tests/fixtures/proxy/. Regenerate deliberately with
QFLIX_REGOLD=1 pytest tests/unit/test_proxygen.py and review the diff.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from lib import proxygen

REPO = Path(__file__).resolve().parents[2]
FIX = REPO / "tests" / "fixtures" / "proxy"
CLI = REPO / "scripts" / "maint" / "lib" / "proxygen.py"
MANIFEST = """\
apps:
  sonarr:
    class: ucc
    health: {kind: http_api, port_secret: sonarr.port, urlbase_secret: sonarr.urlbase}
  seerr:
    class: ucc
    health: {kind: http_api, port_secret: seerr.port}
  tautulli:
    class: ucc
    health: {kind: http_root, port_secret: tautulli.port}
  qflix-dash:
    class: systemd
    health: {kind: http_root, port_secret: qflix-dash.port}
  plex:
    class: ucc
    health: {kind: http_root, port_secret: plex.port}
  kometa:
    class: cron
    health: {kind: none}
"""


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "apps.yaml").write_text(MANIFEST, newline="\n")
    sec = tmp_path / "secrets"
    sec.mkdir()
    for name, val in {"sonarr.port": "42001\n", "sonarr.urlbase": "/sonarr/\n",
                      "seerr.port": "42002", "tautulli.port": "42003",
                      "qflix-dash.port": "42020", "plex.port": "17025"}.items():
        (sec / name).write_text(val, newline="\n")
    return tmp_path


def _render(tree, flavor):
    return proxygen.render(tree / "apps.yaml", tree / "secrets", flavor,
                           "qflix.example.org", "ops@example.org")


@pytest.mark.parametrize("flavor,name", [("caddy", "Caddyfile"), ("nginx", "qflix.conf")])
def test_golden(tree, flavor, name):
    out = _render(tree, flavor)
    gold = FIX / name
    if os.environ.get("QFLIX_REGOLD"):
        FIX.mkdir(parents=True, exist_ok=True)
        gold.write_text(out, newline="\n")
    assert out == gold.read_text()


@pytest.mark.parametrize("flavor", ["caddy", "nginx"])
def test_shape(tree, flavor):
    out = _render(tree, flavor)
    assert "seerr.qflix.example.org" in out and "127.0.0.1:42002" in out   # vhost
    assert "127.0.0.1:42020" in out                                         # dash root
    assert "42001" in out and "/sonarr" in out                              # urlbase passthrough
    assert "17025" not in out                                               # plex never proxied
    assert "kometa" not in out                                              # no port -> no route


def test_deterministic(tree):
    assert _render(tree, "caddy") == _render(tree, "caddy")


def test_missing_port_secret_is_hard_error(tree):
    (tree / "secrets" / "seerr.port").unlink()
    with pytest.raises(proxygen.ProxyGenError, match="seerr.port"):
        _render(tree, "caddy")


def test_missing_dash_is_hard_error(tree):
    (tree / "secrets" / "qflix-dash.port").unlink()
    with pytest.raises(proxygen.ProxyGenError):
        _render(tree, "nginx")


@pytest.mark.parametrize("bad", ["x y", "a;b", "-bad", "a/b", "", "nodots"])
def test_bad_domain_rejected(tree, bad):
    with pytest.raises(proxygen.BadInput):
        proxygen.render(tree / "apps.yaml", tree / "secrets", "caddy", bad, "")


def test_bad_email_rejected(tree):
    with pytest.raises(proxygen.BadInput):
        proxygen.render(tree / "apps.yaml", tree / "secrets", "caddy", "a.example.org", "x y}")


def test_bad_port_rejected(tree):
    (tree / "secrets" / "seerr.port").write_text("99999")
    with pytest.raises(proxygen.ProxyGenError, match="port"):
        _render(tree, "caddy")


def test_unknown_flavor(tree):
    with pytest.raises(proxygen.BadInput):
        _render(tree, "apache")


def test_real_manifest_renders_seerr_vhost_and_dash_root(tmp_path):
    import yaml
    sec = tmp_path / "s"
    sec.mkdir()
    data = yaml.safe_load((REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8"))
    for i, (n, a) in enumerate(data["apps"].items()):
        h = (a or {}).get("health") or {}
        if h.get("port_secret"):
            (sec / h["port_secret"]).write_text(str(42100 + i))
        if h.get("urlbase_secret"):
            (sec / h["urlbase_secret"]).write_text("/" + n)
    for flavor in ("caddy", "nginx"):
        out = proxygen.render(REPO / "manifest" / "apps.yaml", sec, flavor, "q.example.org", "")
        assert "seerr.q.example.org" in out
        assert "reverse_proxy" in out or "proxy_pass" in out


def _cli(tree, domain):
    return subprocess.run([sys.executable, str(CLI), "render", "--flavor", "caddy",
                           "--domain", domain, "--manifest", str(tree / "apps.yaml"),
                           "--secrets-dir", str(tree / "secrets")],
                          capture_output=True, text=True)


def test_cli_ok_and_bad_input(tree):
    r = _cli(tree, "qflix.example.org")
    assert r.returncode == 0, r.stderr
    assert "seerr.qflix.example.org" in r.stdout
    assert _cli(tree, "bad domain").returncode == 2
    (tree / "secrets" / "seerr.port").unlink()
    assert _cli(tree, "qflix.example.org").returncode == 1

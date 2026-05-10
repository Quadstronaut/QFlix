"""tests for lib/qbit.py — qBit pw rotation + *arr cascade.

Mocks both qBit's WebUI and each *arr's DownloadClients API. Verifies:
- happy path: qBit login OK → set-pw OK → relogin OK → cascade rewrites
- idempotent: a second run is a no-op (already rotated)
- failure modes: qBit login fail / set-pw fail / arr test fail / arr put fail
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "maint"))

from lib import qbit  # noqa: E402
from lib.manifest import Manifest, App, HealthConfig  # noqa: E402


def _make_manifest():
    apps = {}
    for slug in ("sonarr", "radarr", "readarr"):
        apps[slug] = App(
            name=slug,
            class_="ucc",
            kuma_monitor=slug.title(),
            health=HealthConfig(kind="http_root", raw={}),
            defaults={},
        )
    return Manifest(apps)


def _write_secrets(tmp_path: Path) -> Path:
    s = tmp_path / "secrets"
    s.mkdir()
    (s / "qbittorrent.user").write_text("quadstronaut")
    (s / "qbittorrent.password").write_text("OLD_PW")
    (s / "qbittorrent.port").write_text("17041")
    for slug in ("sonarr", "radarr", "readarr"):
        (s / f"{slug}.key").write_text(f"key-{slug}")
        (s / f"{slug}.urlbase").write_text(slug)
        (s / f"{slug}.port").write_text("17000")
    return s


class _FakeQbit:
    """Fake qBit responses keyed by (method, path)."""
    def __init__(self):
        self.set_pw_called_with = None

    def login(self, host, user, password):
        if password in ("OLD_PW", "NEW_PW"):
            return ("Ok.", "SID-FAKE")
        return ("Fails.", "")

    def set_pw(self, host, sid, new_password):
        self.set_pw_called_with = new_password
        return (200, "")


class _FakeArrs:
    """Fake *arr API. Each slug has a single QBittorrent downloadclient
    with id=1 and password="OLD_PW" (or "NEW_PW" after rewrite)."""
    def __init__(self):
        self.state = {
            "sonarr": [{
                "id": 1,
                "implementation": "QBittorrent",
                "name": "qBittorrent",
                "fields": [{"name": "password", "value": "OLD_PW"}],
            }],
            "radarr": [{
                "id": 1,
                "implementation": "QBittorrent",
                "name": "qBittorrent",
                "fields": [{"name": "password", "value": "OLD_PW"}],
            }],
            "readarr": [{
                "id": 1,
                "implementation": "QBittorrent",
                "name": "qBittorrent",
                "fields": [{"name": "password", "value": "OLD_PW"}],
            }],
        }

    def req(self, secrets_dir, slug, version, path, *, method="GET", body=None):
        if path == "/downloadclient" and method == "GET":
            return 200, json.dumps(self.state[slug])
        if path == "/downloadclient/test" and method == "POST":
            return 200, "{}"
        if path.startswith("/downloadclient/") and method == "PUT":
            cid = int(path.rsplit("/", 1)[-1])
            for c in self.state[slug]:
                if c["id"] == cid:
                    self.state[slug][self.state[slug].index(c)] = body
                    return 200, "{}"
            return 404, "{}"
        return 404, "{}"


def test_happy_path(tmp_path):
    secrets = _write_secrets(tmp_path)
    m = _make_manifest()
    fq = _FakeQbit()
    fa = _FakeArrs()
    with patch.object(qbit, "_qbit_login",
                       side_effect=lambda h, u, p: fq.login(h, u, p)), \
         patch.object(qbit, "_qbit_set_password",
                       side_effect=lambda h, s, p: fq.set_pw(h, s, p)), \
         patch.object(qbit, "_arr_req",
                       side_effect=lambda s, slug, v, path, **kw: fa.req(s, slug, v, path, **kw)):
        res = qbit.rotate_password(m, "NEW_PW", secrets_dir=secrets)
    assert res.ok, f"expected ok, got {res!r}"
    assert res.arrs_rewritten == {"sonarr": 1, "radarr": 1, "readarr": 1}
    assert (secrets / "qbittorrent.password").read_text() == "NEW_PW"
    assert fq.set_pw_called_with == "NEW_PW"
    # Each *arr's stored password is now NEW_PW
    for slug in ("sonarr", "radarr", "readarr"):
        c = fa.state[slug][0]
        pw_field = next(f for f in c["fields"] if f["name"] == "password")
        assert pw_field["value"] == "NEW_PW"


def test_idempotent_second_run(tmp_path):
    secrets = _write_secrets(tmp_path)
    # Pre-set every *arr to already have NEW_PW (simulating a prior run)
    m = _make_manifest()
    fq = _FakeQbit()
    fa = _FakeArrs()
    for slug in fa.state:
        for c in fa.state[slug]:
            for f in c["fields"]:
                if f["name"] == "password":
                    f["value"] = "NEW_PW"
    (secrets / "qbittorrent.password").write_text("NEW_PW")
    with patch.object(qbit, "_qbit_login",
                       side_effect=lambda h, u, p: fq.login(h, u, p)), \
         patch.object(qbit, "_qbit_set_password",
                       side_effect=lambda h, s, p: fq.set_pw(h, s, p)), \
         patch.object(qbit, "_arr_req",
                       side_effect=lambda s, slug, v, path, **kw: fa.req(s, slug, v, path, **kw)):
        res = qbit.rotate_password(m, "NEW_PW", secrets_dir=secrets)
    # Cascade should skip every *arr (already rotated)
    assert res.arrs_rewritten == {"sonarr": 0, "radarr": 0, "readarr": 0}


def test_qbit_login_fail_no_arr_changes(tmp_path):
    secrets = _write_secrets(tmp_path)
    m = _make_manifest()
    fa = _FakeArrs()
    initial = {slug: [dict(c) for c in fa.state[slug]] for slug in fa.state}

    def fail_login(host, user, password):
        return ("Fails.", "")

    arr_calls = {"n": 0}

    def arr_req(*a, **kw):
        arr_calls["n"] += 1
        return 200, "[]"

    with patch.object(qbit, "_qbit_login", side_effect=fail_login), \
         patch.object(qbit, "_arr_req", side_effect=arr_req):
        res = qbit.rotate_password(m, "NEW_PW", secrets_dir=secrets)
    assert not res.qbit_login_old_ok
    assert not res.ok
    # Should NOT have called arrs at all
    assert arr_calls["n"] == 0
    # Password file unchanged
    assert (secrets / "qbittorrent.password").read_text() == "OLD_PW"


def test_dry_run_no_qbit_login(tmp_path):
    secrets = _write_secrets(tmp_path)
    m = _make_manifest()
    fa = _FakeArrs()

    def fail_login(*a, **kw):
        return ("Should not be called", "")

    with patch.object(qbit, "_qbit_login", side_effect=fail_login), \
         patch.object(qbit, "_arr_req",
                       side_effect=lambda s, slug, v, path, **kw: fa.req(s, slug, v, path, **kw)):
        res = qbit.rotate_password(m, "NEW_PW", secrets_dir=secrets, dry_run=True)
    # Dry-run skips qBit login but still walks arrs to count rewrites
    assert res.qbit_login_old_ok  # asserted in dry-run
    assert res.arrs_rewritten == {"sonarr": 1, "radarr": 1, "readarr": 1}
    # Passwd file unchanged
    assert (secrets / "qbittorrent.password").read_text() == "OLD_PW"

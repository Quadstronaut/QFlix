"""QFLX-96: Tdarr stays off the internet, and its tunnel stays usable.

Tdarr 2.17.01 has no working auth (a keyless /api/v2/cruddb answers 200, `auth`
and `seededApiKey` in its config are inert), so where it listens IS the access
control. Two things went wrong at once and both are pinned here:

  * The server bound the wildcard because it never passes `serverIP` to
    listen(), so the raw port answered on the box's public address. Retiring
    the nginx route alone would have left that open.
  * An nginx /tdarr/ route carried a comment claiming "parent server block
    enforces auth_basic". No such directive exists: it was public.

The patch fixtures below are literal fragments of the vendor file. Keeping them
literal is the point: if the patch script's needles drift from these, the test
fails here instead of on the box.
"""
from __future__ import annotations

import importlib.util
import os
import re

import pytest

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")
_PATCH = os.path.join(_REPO, "scripts", "configure", "50a-tdarr-server-loopback.py")
_INSTALL = os.path.join(_REPO, "scripts", "configure", "50-tdarr-install.sh")
_SMOKE = os.path.join(_REPO, "scripts", "smoke-test.sh")

_BIND_SERVER = "exports['servers']['server']['listen'](serverPort,function(){"
_BIND_UI = "exports[o(0x1cc)]['server2'][o(0x1b4)](webUIPort,function(){"
_REDIRECT = (
    "if(a[l(0x1c9)][l(0x1cd)]['includes'](serverPort)"
    "&&!a['url']['includes'](l(0x1a8))){"
)
_VENDOR = f"'use strict';{_REDIRECT}var x;{_BIND_SERVER}y();{_BIND_UI}z();"


def _load():
    spec = importlib.util.spec_from_file_location("tdarr_loopback", _PATCH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path: str) -> str:
    """Executable shell lines only: comments may quote what was removed."""
    return "\n".join(
        ln for ln in _read(path).splitlines() if not ln.lstrip().startswith("#")
    )


def test_every_listener_is_pinned_to_loopback():
    out = _load().patch_text(_VENDOR)
    assert "(serverPort,/*QFLIX-LOOPBACK-BIND*/'127.0.0.1',function(){" in out
    assert "(webUIPort,/*QFLIX-LOOPBACK-BIND*/'127.0.0.1',function(){" in out
    assert _BIND_SERVER not in out and _BIND_UI not in out


def test_self_redirect_is_gated_on_the_ports_actually_differing():
    """webUIPort == serverPort on this box, so the stock redirect points at the
    URL it was served from: an infinite loop for any Host carrying the port."""
    out = _load().patch_text(_VENDOR)
    assert "/*QFLIX-NO-SELF-REDIRECT*/String(webUIPort)!==String(serverPort)&&" in out
    assert _REDIRECT not in out


def test_patch_is_idempotent():
    mod = _load()
    once = mod.patch_text(_VENDOR)
    assert mod.patch_text(once) == once


@pytest.mark.parametrize("needle", [_BIND_SERVER, _BIND_UI, _REDIRECT])
def test_refuses_a_file_it_does_not_recognise(needle):
    """A half-applied patch would be the worst outcome: a listener still on the
    wildcard behind a marker that smoke reads as fixed."""
    mod = _load()
    with pytest.raises(mod.PatchError):
        mod.patch_text(_VENDOR.replace(needle, "changed-upstream"))


def test_refuses_a_partially_patched_file():
    mod = _load()
    half = mod.patch_text(_VENDOR).replace(
        "(webUIPort,/*QFLIX-LOOPBACK-BIND*/'127.0.0.1',function(){",
        "(webUIPort,function(){",
    ).replace(_BIND_UI, "changed-upstream")
    with pytest.raises(mod.PatchError):
        mod.patch_text(half)


def test_apply_backs_up_before_writing_and_keeps_the_mode(tmp_path):
    target = tmp_path / "servers.js"
    target.write_text(_VENDOR, encoding="utf-8")
    target.chmod(0o644)
    mod = _load()

    assert mod.apply(str(target), stamp="20261102T000000Z") is True
    assert "QFLIX-LOOPBACK-BIND" in target.read_text(encoding="utf-8")
    baks = list(tmp_path.glob("servers.js.qflix-bak-*"))
    assert [b.name for b in baks] == ["servers.js.qflix-bak-20261102T000000Z"]
    assert baks[0].read_text(encoding="utf-8") == _VENDOR

    # Second run changes nothing and must not litter a second backup.
    assert mod.apply(str(target), stamp="20261102T000001Z") is False
    assert len(list(tmp_path.glob("servers.js.qflix-bak-*"))) == 1


def test_apply_leaves_the_file_alone_on_a_refusal(tmp_path):
    target = tmp_path / "servers.js"
    target.write_text("not the vendor file", encoding="utf-8")
    mod = _load()
    with pytest.raises(mod.PatchError):
        mod.apply(str(target), stamp="20261102T000000Z")
    assert target.read_text(encoding="utf-8") == "not the vendor file"
    assert not list(tmp_path.glob("servers.js.qflix-bak-*"))


def test_installer_ships_no_public_route():
    code = _code(_INSTALL)
    assert not re.search(r"location\s+[\^~ ]*/tdarr", code)
    assert "proxy_pass" not in code


def test_installer_retires_a_legacy_route_and_reloads():
    code = _code(_INSTALL)
    assert "proxy.d/tdarr.conf" in code
    assert "nginx -p ~/.apps/nginx/ -c nginx.conf -t" in code
    assert code.index("nginx -p ~/.apps/nginx/ -c nginx.conf -t") < code.index(
        "systemctl --user reload nginx"
    )


def test_installer_patches_before_the_restart_that_loads_it():
    code = _code(_INSTALL)
    assert "50a-tdarr-server-loopback.py" in code
    assert code.index("50a-tdarr-server-loopback.py") < code.index(
        "systemctl --user restart tdarr-server.service"
    )


def test_installer_verify_fails_closed_on_public_exposure():
    code = _code(_INSTALL)
    assert "htpasswd.password" not in code, "nothing public left to log in to"
    assert re.search(r'https://\$\{PUBLIC_HOST\}/tdarr/', code)
    assert re.search(r'http://\$\{PUBLIC_HOST\}:\$\{SERVER_PORT\}/', code)
    assert "die " in code[code.index("/tdarr/"):]


def test_smoke_asserts_the_bind_not_just_the_marker():
    """The marker says the file was edited; only `ss` says the running process
    took it, and a Tdarr restart is what makes the two agree."""
    src = _read(_SMOKE)
    assert "QFLIX-LOOPBACK-BIND" in src
    assert "tdarr-loopback-bound" in src
    assert "tdarr-no-public-route" in src
    assert "ss -ltn" in src

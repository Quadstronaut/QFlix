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

# mainApi.js: the second socket.io instance, the one carrying /webui.
_WEBUI_IO = "sockManager['io']=require(az(0x145))(server2),"
_MAINAPI = (
    "'use strict';var servers_1=require(a7r(0x13c));"
    f"newSocketManager=function(){{var az=a7r;{_WEBUI_IO}"
    "sockManager['io']['of'](az(0x124))['on'](az(0x1ef),f);}"
)


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


def test_webui_shares_the_one_listener_when_the_ports_are_equal():
    """/webui lives on a second socket.io instance bound to server2, which can
    never listen while webUIPort == serverPort (EADDRINUSE, swallowed). The
    browser then gets "Invalid namespace" and every UI panel stays empty."""
    mod = _load()
    out = mod.patch_text(_MAINAPI, mod.MAINAPI_EDITS)
    assert _WEBUI_IO not in out
    assert "/*QFLIX-WEBUI-SHARED-IO*/" in out
    # Same comparison as the redirect gate, same config source as servers.js.
    assert (
        "String(require('../commonModules/configGetter')['configGetter']('webUIPort'))"
        "===String(require('../commonModules/configGetter')['configGetter']('serverPort'))"
    ) in out
    # Equal ports share ioServer; split ports keep the stock second instance.
    assert "?servers_1['ioServer']:require(az(0x145))(server2)," in out


def test_webui_is_never_repointed_at_server():
    """A second instance on `server` steals /socket.io/ from ioServer and /node
    answers "Invalid namespace": the Tdarr Node would drop. Pinned so nobody
    'simplifies' the fix into that."""
    mod = _load()
    out = mod.patch_text(_MAINAPI, mod.MAINAPI_EDITS)
    assert "(servers_1['servers']['server'])" not in out
    assert "['server'])" not in out


def test_apply_all_patches_both_files_with_one_stamp(tmp_path):
    (tmp_path / "servers.js").write_text(_VENDOR, encoding="utf-8")
    (tmp_path / "mainApi.js").write_text(_MAINAPI, encoding="utf-8")
    mod = _load()

    assert mod.apply_all(str(tmp_path), stamp="20261103T000000Z") == [
        "servers.js", "mainApi.js"]
    assert "QFLIX-LOOPBACK-BIND" in (tmp_path / "servers.js").read_text(encoding="utf-8")
    assert "QFLIX-WEBUI-SHARED-IO" in (tmp_path / "mainApi.js").read_text(encoding="utf-8")
    assert (tmp_path / "mainApi.js.qflix-bak-20261103T000000Z").read_text(
        encoding="utf-8") == _MAINAPI
    # Idempotent across both, with no second backup.
    assert mod.apply_all(str(tmp_path), stamp="20261103T000001Z") == []
    assert len(list(tmp_path.glob("*.qflix-bak-*"))) == 2


def test_apply_all_patches_only_what_is_missing(tmp_path):
    """The live box today: servers.js already carries edits 1-3, mainApi.js is
    vendor. Only mainApi.js may be rewritten or backed up."""
    mod = _load()
    (tmp_path / "servers.js").write_text(mod.patch_text(_VENDOR), encoding="utf-8")
    (tmp_path / "mainApi.js").write_text(_MAINAPI, encoding="utf-8")
    assert mod.apply_all(str(tmp_path), stamp="20261103T000000Z") == ["mainApi.js"]
    assert [p.name for p in tmp_path.glob("*.qflix-bak-*")] == [
        "mainApi.js.qflix-bak-20261103T000000Z"]


def test_apply_all_writes_nothing_if_either_file_is_unrecognised(tmp_path):
    """A loopback bind with a broken UI patch, or the reverse, is a state the
    script must never leave: validate every file before writing any."""
    (tmp_path / "servers.js").write_text(_VENDOR, encoding="utf-8")
    (tmp_path / "mainApi.js").write_text("changed upstream", encoding="utf-8")
    mod = _load()
    with pytest.raises(mod.PatchError):
        mod.apply_all(str(tmp_path), stamp="20261103T000000Z")
    assert (tmp_path / "servers.js").read_text(encoding="utf-8") == _VENDOR
    assert not list(tmp_path.glob("*.qflix-bak-*"))


def test_main_refuses_with_exit_1(tmp_path, capsys, monkeypatch):
    """lifecycle runs this as a zip_swap post step: a refusal must be a
    non-zero exit so the upgrade stops before the restart that loads it."""
    (tmp_path / "servers.js").write_text("not vendor", encoding="utf-8")
    (tmp_path / "mainApi.js").write_text(_MAINAPI, encoding="utf-8")
    mod = _load()
    monkeypatch.setattr(mod.sys, "argv", ["50a", str(tmp_path)])
    assert mod.main() == 1
    assert "REFUSED" in capsys.readouterr().err


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


def test_smoke_speaks_socket_io_to_webui():
    """REST answered all along while the UI was empty: only a real /webui
    CONNECT tells the two apart. Both verdict packets must be told apart."""
    src = _read(_SMOKE)
    assert "tdarr-webui-namespace" in src
    assert 'b"40/webui,"' in src
    assert '"40/webui," in reply' in src and '"44/webui," in reply' in src
    # /node's register middleware has side effects; the gate must not touch it.
    assert '"40/node' not in src

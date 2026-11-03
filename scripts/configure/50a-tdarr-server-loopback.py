#!/usr/bin/env python3
"""QFLX-96: keep Tdarr 2.17.01 Server off the internet, and usable over a tunnel.

Runs ON THE BOX, piped from the workstation (same as the worker1.js patch in
50-tdarr-install.sh):

    sshm 'python3 -' < scripts/configure/50a-tdarr-server-loopback.py

or, once 240-maintenance-install.sh has shipped it, as the tdarr-server
`upgrade.post_steps` in manifest/apps.yaml (lifecycle zip_swap re-unzips the
vendor tree, so the patch must ride every upgrade, downgrade and rollback):

    python3 ~/scripts/configure/50a-tdarr-server-loopback.py

Idempotent. Takes effect on the next tdarr-server restart. A Tdarr upgrade or
re-unzip restores the vendor files, so 50-tdarr-install.sh and the lifecycle
post step re-apply this, and smoke-test.sh asserts the markers, the live bind
and a real /webui handshake. A refusal exits 1, which fails the post step
BEFORE lifecycle restarts the unit.

Why patch at all: Tdarr 2.17.01 has NO working auth. `auth` and `seededApiKey`
in its config are inert, and a keyless POST /api/v2/cruddb answers 200, so
whoever reaches the port owns the database. Where it listens is the only access
control there is, and it cannot be moved upstream of the process: the slot has
no firewall access, and GLIBC 2.31 caps the version at 2.17.01.

The three edits, all in srcug/api/servers.js:

  1 and 2. Bind loopback. listen(port, cb) passes no host, so Node takes the
     wildcard and the raw port answers on the box's public address. `serverIP`
     and the unit's HOST= look like bind settings; serverIP is only ever logged
     and HOST is never read. Hardcoded rather than read from `serverIP` so a
     rewritten config fails closed instead of republishing the port.

  3. Stop the self-redirect. Any request whose Host contains serverPort, unless
     the URL has "api/v2", is redirected to http://<host>:<webUIPort><url>. That
     is the stock split-port hand-off, but this box pins webUIPort == serverPort
     (see 50b-tdarr-config.py), so it redirects to itself forever. A tunnel
     always sends Host: localhost:<port>, which is why the UI looped while
     /api/v2/status answered, and why nginx (Host with no port) never saw it.
     The gate keeps the stock behaviour if the ports are ever split again.

And one in srcug/api/mainApi.js:

  4. Give /webui a listener. Tdarr builds TWO socket.io servers: servers.js
     attaches `ioServer` (namespaces / and /node) to `server`, and mainApi.js
     attaches a second one, carrying /webui and every UI sync handler
     (syncNodes, syncResStats, library and flow sync), to `server2`. With
     webUIPort == serverPort, server2.listen() hits EADDRINUSE, the vendor
     error handler swallows it, and server2 never binds for the life of the
     process. Every browser socket lands on `server`, whose instance has no
     /webui, and gets CONNECT_ERROR "Invalid namespace": the UI loads but
     Nodes, OS Mem, Libraries and Flows stay empty while REST answers fine.
     Logged on 18 of 18 starts from 2026-08-20, on the wildcard before this
     patch and on 127.0.0.1 after it, so edits 1-3 did not cause it.
     Fix: when the ports are equal, register /webui on `ioServer`. Namespaces
     are independent, so /node's register middleware never runs for /webui.
     Repointing the second instance at `server` instead would NOT work: two
     socket.io servers on one http server fight over /socket.io/ and /node
     answers "Invalid namespace" — that breaks the Tdarr Node.
     Same gate as edit 3, so a split-port config keeps stock behaviour.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_API_DIR = "~/.apps/tdarr/Tdarr_Server/srcug/api"

BIND = "/*QFLIX-LOOPBACK-BIND*/'127.0.0.1',"
WEBUI_MARK = "QFLIX-WEBUI-SHARED-IO"

# (vendor text, replacement). Exact and whole-statement on purpose: the files
# are obfuscated, so anything looser could match the wrong call.
EDITS = (
    (
        "exports['servers']['server']['listen'](serverPort,function(){",
        f"exports['servers']['server']['listen'](serverPort,{BIND}function(){{",
    ),
    (
        "exports[o(0x1cc)]['server2'][o(0x1b4)](webUIPort,function(){",
        f"exports[o(0x1cc)]['server2'][o(0x1b4)](webUIPort,{BIND}function(){{",
    ),
    (
        "if(a[l(0x1c9)][l(0x1cd)]['includes'](serverPort)"
        "&&!a['url']['includes'](l(0x1a8))){",
        "if(/*QFLIX-NO-SELF-REDIRECT*/String(webUIPort)!==String(serverPort)&&"
        "a[l(0x1c9)][l(0x1cd)]['includes'](serverPort)"
        "&&!a['url']['includes'](l(0x1a8))){",
    ),
)

# Edit 4, mainApi.js. Decoded from the 2.17.01 string table (not run): `az` is
# the decoder, az(0x145) == 'socket.io', and `servers_1` is require('./servers'),
# whose exports carry `ioServer`. Ports are read the way servers.js reads them,
# through ../commonModules/configGetter, so the gate compares the same values.
_CFG = "require('../commonModules/configGetter')['configGetter']"
MAINAPI_EDITS = (
    (
        "sockManager['io']=require(az(0x145))(server2),",
        f"sockManager['io']=/*{WEBUI_MARK}*/"
        f"String({_CFG}('webUIPort'))===String({_CFG}('serverPort'))"
        "?servers_1['ioServer']:require(az(0x145))(server2),",
    ),
)

# File name under the api dir -> its edits. Order is the order they are
# written; every file is validated before any is written.
TARGETS = (("servers.js", EDITS), ("mainApi.js", MAINAPI_EDITS))


class PatchError(RuntimeError):
    """The file is not the 2.17.01 we know. Refuse rather than half-patch."""


def patch_text(txt: str, edits=EDITS) -> str:
    """Return the patched text. Every edit for this file lands or none do.

    A half-applied patch is the failure to design out: a listener still on the
    wildcard behind a marker that smoke reads as fixed.
    """
    if all(new in txt for _, new in edits):
        return txt
    missing = [old for old, _ in edits if txt.count(old) != 1]
    if missing:
        raise PatchError(
            f"{len(missing)} of {len(edits)} needles not found exactly once: "
            "Tdarr_Server layout changed or the patch is partial. Nothing written."
        )
    for old, new in edits:
        txt = txt.replace(old, new)
    return txt


def _write(path: str, out: str, stamp: str) -> None:
    shutil.copy2(path, f"{path}.qflix-bak-{stamp}")
    # Beside the target, then rename: a process reading the file mid-write gets
    # the old inode or the new one, never a torn file.
    tmp = f"{path}.qflix-new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(out)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)


def apply(path: str, stamp: str, edits=EDITS) -> bool:
    """Patch one file in place. Returns True if it changed anything."""
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    out = patch_text(txt, edits)
    if out == txt:
        return False
    _write(path, out, stamp)
    return True


def apply_all(api_dir: str, stamp: str) -> list:
    """Patch every target under `api_dir`. Returns the names it changed.

    All files are read and patched in memory FIRST, so a refusal on the second
    file leaves the first untouched too: a loopback bind without a working UI,
    or the reverse, is not a state this script can leave behind.
    """
    planned = []
    for name, edits in TARGETS:
        path = os.path.join(api_dir, name)
        with open(path, encoding="utf-8") as fh:
            txt = fh.read()
        out = patch_text(txt, edits)
        if out != txt:
            planned.append((name, path, out))
    for _, path, out in planned:
        _write(path, out, stamp)
    return [name for name, _, _ in planned]


def main() -> int:
    api_dir = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_API_DIR)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        changed = apply_all(api_dir, stamp)
    except (OSError, PatchError) as exc:
        print(f"tdarr loopback patch REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        f"tdarr patch applied to {', '.join(changed)} (backup .qflix-bak-{stamp}); restart needed"
        if changed
        else "tdarr patch already present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

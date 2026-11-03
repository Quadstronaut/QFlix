#!/usr/bin/env python3
"""QFLX-96 + QFLX-97: Tdarr 2.17.01 Server gets no TCP port at all.

Runs ON THE BOX, piped from the workstation (same as the worker1.js patch in
50-tdarr-install.sh):

    sshm 'python3 -' < scripts/configure/50a-tdarr-server-loopback.py

or, once 240-maintenance-install.sh has shipped it, as the tdarr-server
`upgrade.post_steps` in manifest/apps.yaml (lifecycle zip_swap re-unzips the
vendor tree, so the patch must ride every upgrade, downgrade and rollback):

    python3 ~/scripts/configure/50a-tdarr-server-loopback.py

Idempotent. Takes effect on the next tdarr-server restart. A Tdarr upgrade or
re-unzip restores the vendor files, so 50-tdarr-install.sh and the lifecycle
post step re-apply this; smoke-test.sh and the tdarr-loopback-bind canary
assert the markers, that no Tdarr process owns a TCP listener, and a real
/webui handshake. A refusal exits 1, which fails the post step BEFORE
lifecycle restarts the unit. (The file name predates QFLX-97 and is kept
because the manifest, the installers and deploy-drift all name it.)

Why patch at all: Tdarr 2.17.01 has NO working auth. `auth` and `seededApiKey`
in its config are inert, and a keyless POST /api/v2/cruddb answers 200, so
whoever reaches the listener owns the database and runs code as our uid. Where
it listens is the only access control there is.

  QFLX-96 bound it to 127.0.0.1, which took it off the internet. It did NOT
  make it private: every tenant on this slot shares the host network
  namespace, so 127.0.0.1:<port> is reachable by ~150 other uids.

  QFLX-97 takes the TCP port away. Tdarr listens on a unix socket in
  /run/user/<uid>/qflix-tdarr (0700, checked to be ours), which no other uid
  can traverse. scripts/ops/tdarr-uid-relay.py holds 127.0.0.1:<serverPort>
  and forwards only connections whose peer socket the kernel says is ours,
  so Tdarr Node and every API caller keep using the same address.

The edits, in srcug/api/servers.js:

  1 and 2. Listen on the unix socket. `server` takes tdarr.sock (the one the
     relay forwards to); `server2` takes its own tdarr-webui.sock, which never
     receives traffic while webUIPort == serverPort (edit 4 moves /webui off
     it) but must not share a path: binding it would unlink tdarr.sock. The
     helper creates the directory 0700, refuses one we do not own, and removes
     a stale socket file (a unix socket path outlives its process). Any throw
     happens before listen(): Tdarr fails to start rather than listen
     somewhere else. Each listener also reports remoteAddress 127.0.0.1 for
     its connections, which is what it saw before (nodeRelay stores it on the
     node record) and is true: everything on the socket came through the
     relay from loopback. Accepts the vendor text or the QFLX-96 loopback form.

  3. Stop the self-redirect. Any request whose Host contains serverPort, unless
     the URL has "api/v2", is redirected to http://<host>:<webUIPort><url>. That
     is the stock split-port hand-off, but this box pins webUIPort == serverPort
     (see 50b-tdarr-config.py), so it redirects to itself forever. The gate
     keeps the stock behaviour if the ports are ever split again.

And one in srcug/api/mainApi.js:

  4. Give /webui a listener. Tdarr builds TWO socket.io servers: servers.js
     attaches `ioServer` (namespaces / and /node) to `server`, and mainApi.js
     attaches a second one, carrying /webui and every UI sync handler, to
     `server2`. With webUIPort == serverPort that second server never got
     traffic, so the browser got "Invalid namespace" and Nodes, OS Mem,
     Libraries and Flows stayed empty. Fix: when the ports are equal,
     register /webui on `ioServer`. Repointing the second instance at `server`
     instead would NOT work: two socket.io servers on one http server fight
     over /socket.io/ and /node answers "Invalid namespace", which drops the
     Tdarr Node.
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_API_DIR = "~/.apps/tdarr/Tdarr_Server/srcug/api"

UNIX_MARK = "QFLIX-UNIX-SOCKET"
WEBUI_MARK = "QFLIX-WEBUI-SHARED-IO"
SOCK_DIR_JS = "'/run/user/'+process.getuid()+'/qflix-tdarr'"

# The QFLX-96 form, accepted as a needle so a box patched by it migrates.
_OLD_BIND = "/*QFLIX-LOOPBACK-BIND*/'127.0.0.1',"


def _sock_expr(server_expr: str, name: str) -> str:
    """JS: make the private dir, clear a stale socket, return its path.

    448 is 0o700. The uid check refuses a directory someone else made (it
    cannot happen under a 0700 /run/user/<uid>, but a check is cheaper than an
    argument). 'connection' rewrites remoteAddress once per server object, so
    a restartServer() cycle does not stack listeners.
    """
    return (
        f"/*{UNIX_MARK}*/(function(s,n){{var f=require('fs'),d={SOCK_DIR_JS};"
        "f.mkdirSync(d,{recursive:true,mode:448});"
        "var t=f.lstatSync(d);if(!t.isDirectory()||t.uid!==process.getuid())"
        "throw new Error('qflix: '+d+' is not our directory');"
        "f.chmodSync(d,448);var p=d+'/'+n;try{f.unlinkSync(p)}catch(e){}"
        "if(!s.__qflixRA){s.__qflixRA=1;s.on('connection',function(c){try{"
        "Object.defineProperty(c,'remoteAddress',{value:'127.0.0.1',configurable:true});"
        "Object.defineProperty(c,'remoteFamily',{value:'IPv4',configurable:true});"
        f"}}catch(e){{}}}})}}return p}})({server_expr},'{name}'),"
    )


_SRV = "exports['servers']['server']"
_SRV2 = "exports[o(0x1cc)]['server2']"

# (accepted needles, replacement). Exact and whole-statement on purpose: the
# files are obfuscated, so anything looser could match the wrong call.
EDITS = (
    (
        (
            f"{_SRV}['listen'](serverPort,function(){{",
            f"{_SRV}['listen'](serverPort,{_OLD_BIND}function(){{",
        ),
        f"{_SRV}['listen']({_sock_expr(_SRV, 'tdarr.sock')}function(){{",
    ),
    (
        (
            f"{_SRV2}[o(0x1b4)](webUIPort,function(){{",
            f"{_SRV2}[o(0x1b4)](webUIPort,{_OLD_BIND}function(){{",
        ),
        f"{_SRV2}[o(0x1b4)]({_sock_expr(_SRV2, 'tdarr-webui.sock')}function(){{",
    ),
    (
        (
            "if(a[l(0x1c9)][l(0x1cd)]['includes'](serverPort)"
            "&&!a['url']['includes'](l(0x1a8))){",
        ),
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
        ("sockManager['io']=require(az(0x145))(server2),",),
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

    A half-applied patch is the failure to design out: one listener on a TCP
    port behind a marker that smoke reads as fixed. Each edit must find exactly
    one of its needles exactly once, or already be present.
    """
    plan = []
    for olds, new in edits:
        if new in txt:
            continue
        hits = [old for old in olds if txt.count(old) == 1]
        if len(hits) != 1 or any(txt.count(old) > 1 for old in olds):
            raise PatchError(
                f"needle for edit {edits.index((olds, new)) + 1} of {len(edits)} not found "
                "exactly once: Tdarr_Server layout changed or the patch is partial. "
                "Nothing written."
            )
        plan.append((hits[0], new))
    for old, new in plan:
        txt = txt.replace(old, new)
    if any(new not in txt for _, new in edits):
        raise PatchError("patch did not land completely. Nothing written.")
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
    file leaves the first untouched too.
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
        print(f"tdarr unix-socket patch REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        f"tdarr patch applied to {', '.join(changed)} (backup .qflix-bak-{stamp}); restart needed"
        if changed
        else "tdarr patch already present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

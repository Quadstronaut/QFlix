#!/usr/bin/env python3
"""QFLX-96: keep Tdarr 2.17.01 Server off the internet, and usable over a tunnel.

Runs ON THE BOX, piped from the workstation (same as the worker1.js patch in
50-tdarr-install.sh):

    sshm 'python3 -' < scripts/configure/50a-tdarr-server-loopback.py

Idempotent. Takes effect on the next tdarr-server restart. A Tdarr upgrade or
re-unzip restores the vendor file, so 50-tdarr-install.sh re-applies this and
smoke-test.sh asserts both the marker and the live bind.

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
"""
from __future__ import annotations

import os
import shutil
import sys
import time

DEFAULT_TARGET = "~/.apps/tdarr/Tdarr_Server/srcug/api/servers.js"

BIND = "/*QFLIX-LOOPBACK-BIND*/'127.0.0.1',"

# (vendor text, replacement). Exact and whole-statement on purpose: the file is
# obfuscated, so anything looser could match the wrong call.
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


class PatchError(RuntimeError):
    """The file is not the 2.17.01 we know. Refuse rather than half-patch."""


def patch_text(txt: str) -> str:
    """Return the patched text. All three edits land or none do.

    A half-applied patch is the failure to design out: a listener still on the
    wildcard behind a marker that smoke reads as fixed.
    """
    if all(new in txt for _, new in EDITS):
        return txt
    missing = [old for old, _ in EDITS if txt.count(old) != 1]
    if missing:
        raise PatchError(
            f"{len(missing)} of {len(EDITS)} needles not found exactly once: "
            "Tdarr_Server layout changed or the patch is partial. Nothing written."
        )
    for old, new in EDITS:
        txt = txt.replace(old, new)
    return txt


def apply(path: str, stamp: str) -> bool:
    """Patch `path` in place. Returns True if it changed anything."""
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    out = patch_text(txt)
    if out == txt:
        return False
    shutil.copy2(path, f"{path}.qflix-bak-{stamp}")
    # Beside the target, then rename: a process reading the file mid-write gets
    # the old inode or the new one, never a torn file.
    tmp = f"{path}.qflix-new"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(out)
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return True


def main() -> int:
    path = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TARGET)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    try:
        changed = apply(path, stamp)
    except (OSError, PatchError) as exc:
        print(f"tdarr loopback patch REFUSED: {exc}", file=sys.stderr)
        return 1
    print(
        f"tdarr loopback patch applied (backup .qflix-bak-{stamp}); restart needed"
        if changed
        else "tdarr loopback patch already present"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

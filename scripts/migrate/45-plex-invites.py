#!/usr/bin/env python3
"""45-plex-invites.py -- mirror blue's per-member Plex library share SET onto green.

Green (NEW) is a new Plex server identity: nobody is shared on it yet. For
every person who has an accepted share on blue (OLD) this reads the share's
ACTUAL section titles through plexapi and invites the same person to green
with the SAME titles. Sections are matched BY LIBRARY NAME.

NO POLICY IS RE-DERIVED. This never reads members.yaml and never decides who
"should" see what -- the entitlement gate owns that. It copies what Plex says
is TRUE on blue today. Tagalong (plex_only) accounts are ordinary Plex
friends of blue, so they are mirrored like everyone else; they hold no Seerr
or newsletter rows and this script touches neither.

GREEN GATE MUST BE DISARMED (I-5). Before any --execute write the script asks
green (read-only ssh to NEW_HOST) for the gate state and REFUSES unless the
roster says an explicit `armed: false` AND no entitlement service drop-in
carries `--execute`. Anything unreadable also refuses (fail closed): an armed
gate on a freshly-invited green would reconcile members against a half-built
box.

PRIVACY (public repo). Output is masked addresses and counts only. Hosts come
from argv or gitignored secrets, never from this file.

SAFETY. Default is a masked dry-run that writes nothing; --execute acts.
Blue is read-only (one `curl /identity` via scripts/lib/ssh.sh). Every write
targets only green's machineIdentifier.

plex.tv QUIRK: the invite POST can answer 400 yet CREATE the invite. A raised
error is therefore not counted as a failure until /api/invites/requested
(plexapi pendingInvites) has been checked for that person.

USAGE
  python3 scripts/migrate/45-plex-invites.py NEW_HOST [--execute]
  NEW_HOST: the green ssh host (gate probe only); falls back to
  secrets/new-host.ssh-host. OLD_HOST is not needed: blue's machine id comes
  from `sshm` (secrets/seedbox.ssh-host) or --old-machine-id.

EXIT CODES: 0 ok * 1 per-user failure/skip (re-run is safe) * 2 cannot assert
(no token, no plex.tv, gate not provably disarmed, ids unresolved).
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Dict, List, Optional

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CANNOT_ASSERT = 2

_HERE = Path(__file__).resolve()

# Read-only probe run on green. Prints ROSTER_ARMED=<value|MISSING>, one
# DROPIN=<file> per drop-in that carries --execute, then PROBE_END.
GATE_PROBE_CMD = r'''r="$HOME/secrets/members.yaml"
if [ -f "$r" ]; then v=$(grep -E "^armed:" "$r" | head -1 | sed "s/^armed:[[:space:]]*//; s/[[:space:]]*#.*//"); echo "ROSTER_ARMED=$v"; else echo ROSTER_ARMED=MISSING; fi
for f in "$HOME"/.config/systemd/user/manitoba-maint-entitlement.service.d/*.conf; do [ -f "$f" ] && grep -q -- --execute "$f" && echo "DROPIN=$(basename "$f")"; done
echo PROBE_END'''


def _secrets_dir() -> Path:
    env = os.environ.get("MANITOBA_SECRETS_DIR") or os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env).expanduser()
    try:
        repo_secrets = _HERE.parents[2] / "secrets"
        if repo_secrets.is_dir():
            return repo_secrets
    except IndexError:
        pass
    return Path.home() / "secrets"


def _read_secret(name: str, required: bool = True) -> str:
    p = _secrets_dir() / name
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        if required:
            print("missing secret: %s" % p, file=sys.stderr)
            sys.exit(EXIT_CANNOT_ASSERT)
        return ""


def _mask_email(addr: str) -> str:
    """First char of local part and domain only -- never print a real address."""
    local, _, domain = (addr or "").partition("@")
    if not domain or not local:
        return "***"
    parts = domain.split(".")
    tld = ".".join(parts[1:])
    return "%s***@%s" % (local[:1], parts[0][:1] + "***" + ("." + tld if tld else ""))


# ---------------------------------------------------------------- gate (I-5)

def parse_gate_probe(output: str):
    """(disarmed: bool, reason). Fail closed on anything but explicit false."""
    lines = [l.strip() for l in (output or "").splitlines()]
    if "PROBE_END" not in lines:
        return False, "gate probe output incomplete (no PROBE_END)"
    armed = None
    dropins = []
    for l in lines:
        if l.startswith("ROSTER_ARMED="):
            armed = l.split("=", 1)[1].strip().strip("'\"").lower()
        elif l.startswith("DROPIN="):
            dropins.append(l.split("=", 1)[1])
    if dropins:
        return False, "execute drop-in present on green: %s" % ", ".join(dropins)
    if armed is None:
        return False, "gate probe did not report the roster armed state"
    if armed == "missing":
        return False, "green roster not found; armed:false must be explicit"
    if armed != "false":
        return False, "green roster armed is %r, not false" % armed
    return True, "armed:false and no --execute drop-in"


def probe_gate_over_ssh(new_host: str) -> str:
    """Read-only ssh to green as quadstronaut (the only permitted user)."""
    target = new_host if "@" in new_host else "quadstronaut@" + new_host
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", target, GATE_PROBE_CMD],
        capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("gate probe ssh failed rc=%d" % proc.returncode)
    return proc.stdout


def assert_gate_disarmed(new_host: Optional[str],
                         runner: Callable[[str], str] = probe_gate_over_ssh):
    if not new_host:
        return False, "no NEW_HOST given; cannot probe green's gate"
    try:
        out = runner(new_host)
    except Exception as e:  # unreachable == unprovable == refuse
        return False, "could not probe green's gate (%s)" % e
    return parse_gate_probe(out)


# -------------------------------------------------------------- blue / green

def discover_blue_machine_id(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    port = _read_secret("plex.port", required=False) or "32400"
    curl = "curl -fsS --max-time 10 http://127.0.0.1:%s/identity" % shlex.quote(port)
    ssh_lib = _HERE.parents[1] / "lib" / "ssh.sh"
    cmd = ("source %s && sshm %s" % (shlex.quote(str(ssh_lib)), shlex.quote(curl))
           if ssh_lib.is_file() else curl)
    try:
        proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("could not reach blue (%s). Pass --old-machine-id." % e, file=sys.stderr)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        print("blue /identity probe failed rc=%d" % proc.returncode, file=sys.stderr)
        return None
    try:
        return ET.fromstring(proc.stdout).get("machineIdentifier")
    except ET.ParseError:
        print("blue /identity was not valid XML", file=sys.stderr)
        return None


def resolve_green_machine_id(account, old_id: str, explicit: Optional[str]):
    if explicit:
        return explicit, "explicit --new-machine-id"
    servers = [r for r in account.resources()
               if getattr(r, "owned", False) and "server" in (getattr(r, "provides", "") or "").split(",")]
    cands = [r for r in servers if r.clientIdentifier != old_id]
    if not cands:
        return None, "no other Plex Media Server owned by this account (green not claimed?)"
    if len(cands) > 1:
        return None, "ambiguous: %d non-blue servers -- pass --new-machine-id" % len(cands)
    return cands[0].clientIdentifier, "resolved by elimination"


def server_section_titles(account, machine_id: str) -> Optional[List[str]]:
    """Library titles of a server via plex.tv resource connect; None if unreachable."""
    try:
        res = next(r for r in account.resources() if r.clientIdentifier == machine_id)
        return sorted(s.title for s in res.connect().library.sections())
    except Exception:
        return None


def share_titles(share, old_all_titles: Optional[List[str]]):
    """(titles|None, detail). allLibraries expands to blue's full section list."""
    if getattr(share, "allLibraries", False):
        if old_all_titles:
            return sorted(old_all_titles), "allLibraries expanded to blue's %d sections" % len(old_all_titles)
        return None, "allLibraries share but blue's section list is unavailable"
    titles = sorted({s.title.strip() for s in share.sections()})
    if not titles:
        return None, "share has zero sections"
    return titles, "%d section(s)" % len(titles)


def build_plan(account, old_id: str, green_titles: Optional[List[str]]) -> List[Dict]:
    old_all = server_section_titles(account, old_id)
    green_lower = {t.lower() for t in green_titles} if green_titles is not None else None
    rows: List[Dict] = []
    for user in account.users():
        share = next((s for s in (getattr(user, "servers", None) or [])
                      if getattr(s, "machineIdentifier", None) == old_id), None)
        if share is None:
            continue
        email = user.email or user.username or ("id:%s" % user.id)
        titles, detail = share_titles(share, old_all)
        row = {"user": user, "email": email, "masked": _mask_email(email),
               "titles": titles, "detail": detail, "kind": "mirror"}
        if titles is None:
            row["kind"] = "anomalous"
        elif green_lower is not None:
            missing = [t for t in titles if t.lower() not in green_lower]
            if missing:
                row["kind"] = "anomalous"
                row["detail"] = "%d section(s) absent on green by name" % len(missing)
        rows.append(row)
    return rows


def print_plan(rows, green_id, note, gate_note) -> None:
    print("=== 45-plex-invites: dry-run plan ===")
    print("green machineIdentifier: %s (%s)" % (green_id or "UNRESOLVED", note))
    print("green gate: %s" % gate_note)
    for r in rows:
        want = "%d section(s)" % len(r["titles"]) if r["titles"] else "SKIP"
        print("  %-28s kind=%-9s -> %s | %s" % (r["masked"], r["kind"], want, r["detail"]))
    n = sum(1 for r in rows if r["kind"] == "mirror")
    print("\n%d blue share(s); %d would be mirrored, %d skipped" % (len(rows), n, len(rows) - n))


def _pending_emails(account) -> set:
    """Emails with an outstanding SENT invite (/api/invites/requested)."""
    out = set()
    for inv in account.pendingInvites(includeSent=True, includeReceived=False):
        for attr in ("email", "username"):
            v = getattr(inv, attr, None)
            if v:
                out.add(str(v).lower())
    return out


def existing_green_share(user, green_id):
    for s in getattr(user, "servers", None) or []:
        if getattr(s, "machineIdentifier", None) == green_id:
            return s
    return None


def execute_plan(account, rows, green_id) -> int:
    ok = fail = skipped = 0
    for r in rows:
        if r["kind"] != "mirror":
            print("SKIP  %s: %s" % (r["masked"], r["detail"]))
            skipped += 1
            continue
        desired = r["titles"]
        existing = existing_green_share(r["user"], green_id)
        try:
            if existing is not None:
                have = {s.title.lower() for s in existing.sections()}
                if have == {t.lower() for t in desired}:
                    print("SKIP  %s: green share already equals blue's set" % r["masked"])
                    skipped += 1
                    continue
                account.updateFriend(user=r["email"], server=green_id, sections=desired)
                print("UPDATE %s" % r["masked"])
                ok += 1
                continue
            if r["email"].lower() in _pending_emails(account):
                print("SKIP  %s: invite already pending on plex.tv" % r["masked"])
                skipped += 1
                continue
            try:
                # allowSync=True: every member may download (QFLX-49); plexapi
                # defaults it to False, which is how shares drift off.
                account.inviteFriend(user=r["email"], server=green_id,
                                     sections=desired, allowSync=True)
                print("INVITE %s" % r["masked"])
            except Exception as e:
                # 400 may still have created the invite: verify before failing.
                if r["email"].lower() in _pending_emails(account):
                    print("INVITE %s (plex.tv errored but invite verified pending)" % r["masked"])
                else:
                    raise e
            ok += 1
        except Exception as e:
            print("FAIL  %s: %s" % (r["masked"], type(e).__name__), file=sys.stderr)
            fail += 1
    print("\ndone: %d ok, %d failed, %d skipped" % (ok, fail, skipped))
    return EXIT_OK if fail == 0 and skipped == 0 else EXIT_PARTIAL


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("new_host", nargs="?", default=None, help="green ssh host (gate probe)")
    ap.add_argument("--execute", action="store_true", help="act (default: masked dry-run)")
    ap.add_argument("--old-machine-id", default=None)
    ap.add_argument("--new-machine-id", default=None)
    args = ap.parse_args(argv)
    try:
        from plexapi.myplex import MyPlexAccount
    except ImportError:
        print("plexapi not importable -- pip install plexapi", file=sys.stderr)
        return EXIT_CANNOT_ASSERT

    new_host = args.new_host or _read_secret("new-host.ssh-host", required=False) or None

    # Gate first: a refusal must precede every plex.tv write.
    disarmed, gate_note = (False, "not probed (dry-run, no NEW_HOST)")
    if args.execute or new_host:
        disarmed, gate_note = assert_gate_disarmed(new_host)
    if args.execute and not disarmed:
        print("REFUSING: green gate not provably disarmed: %s" % gate_note, file=sys.stderr)
        return EXIT_CANNOT_ASSERT

    token = _read_secret("plex.token")
    old_id = discover_blue_machine_id(args.old_machine_id)
    if not old_id:
        print("could not determine blue's machineIdentifier.", file=sys.stderr)
        return EXIT_CANNOT_ASSERT
    try:
        account = MyPlexAccount(token=token)
    except Exception as e:
        print("could not authenticate to plex.tv: %s" % type(e).__name__, file=sys.stderr)
        return EXIT_CANNOT_ASSERT

    green_id, note = resolve_green_machine_id(account, old_id, args.new_machine_id)
    green_titles = server_section_titles(account, green_id) if green_id else None
    rows = build_plan(account, old_id, green_titles)
    if not args.execute:
        print_plan(rows, green_id, note, gate_note)
        return EXIT_OK
    if not green_id:
        print("--execute needs a resolved green id (%s)." % note, file=sys.stderr)
        return EXIT_CANNOT_ASSERT
    if green_titles is None:
        print("--execute needs green's library list to mirror by name; green unreachable.",
              file=sys.stderr)
        return EXIT_CANNOT_ASSERT
    return execute_plan(account, rows, green_id)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""45-plex-invites.py -- re-invite blue's Plex friends to green, mirroring
the entitlement gate's own two-tier access shape.

Green is a NEW Plex server identity (spec sec 2.4): nobody is shared on it
yet. This reads blue's LIVE friend list and replicates each person's CURRENT
tier onto green: full access -> every green section except Welcome; Welcome
only -> just Welcome (the gate's "not entitled" floor; see lib/plexshare.py
full_access_ids()/minimum_access_ids() on the box).

Deliberately does NOT read members.yaml (box-only; lib/members.py refuses
tracked dirs). Re-deriving its gate math here would be a second policy
surface computing the same answer a drift-prone second way -- the exact bug
class this repo keeps naming. This asks Plex what is TRUE today (the live
share) instead of recomputing an opinion about what SHOULD be true.

BLUE IS READ-ONLY (I-2): the only blue traffic is one `curl .../identity`
over the existing `sshm` helper (scripts/lib/ssh.sh), sourced and reused so
host resolution can't drift out of sync with it. Every write targets ONLY
green's machineIdentifier. SHIPS INERT (I-3): default prints the plan with
masked emails and writes nothing; `--execute` performs invites/updates.

RUNS TWO WAYS (detected -- see the plexapi ImportError branch in main()):
  locally  python3 scripts/migrate/45-plex-invites.py [--execute]
  on blue  sshm '~/.apps/python-plexapi/venv/bin/python \
                 ~/.opt/qflix-src/scripts/migrate/45-plex-invites.py --execute'
  (checkout path, not ~/scripts: migrate/ is one-shot tooling, not
  deploy-drift-tracked runtime code.)

NEEDS NO SSH TO GREEN AND NO NEW_HOST. plex.token is an IDENTITY secret
(spec sec 2.3) -- one value claims both boxes -- so green is found by asking
plex.tv for every Plex Media Server this account owns and taking whichever
one is NOT blue. NEW_HOST is accepted positionally (unused) only so
50-cutover.sh can call every migrate/ script with one uniform argv shape;
`--new-machine-id` overrides the lookup when ambiguous or unclaimed.

EXIT CODES: 0 ok * 1 a per-user action failed or was skipped as anomalous
(re-run is safe, I-4) * 2 could not assert (no token/plex.tv reach, or
--execute with no resolvable green id / section set).
"""
from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CANNOT_ASSERT = 2

DEFAULT_WELCOME_TITLE = "QFlix - Welcome"
_HERE = Path(__file__).resolve()

# Mirrors scripts/maint/lib/secrets.py's resolution order (env override ->
# repo-relative secrets/ -> ~/secrets/) without importing it -- that module
# is a different subsystem, and this script is meant to run standalone.
def _secrets_dir() -> Path:
    import os
    env = os.environ.get("MANITOBA_SECRETS_DIR") or os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env).expanduser()
    # scripts/migrate/../.. -> repo root; when copied somewhere shallow
    # (/tmp on the box) parents[2] does not exist -- fall through to ~/secrets.
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
    """Only the first char of the local part and domain survive -- never
    print a real address (repo-wide rule; test_no_pii_in_repo.py)."""
    local, _, domain = addr.partition("@")
    if not domain or not local:
        return "***"
    dom_parts = domain.split(".")
    tld = ".".join(dom_parts[1:])
    masked_domain = (dom_parts[0][:1] + "***" + ("." + tld if tld else ""))
    return "%s***@%s" % (local[:1], masked_domain)

def discover_blue_machine_id(explicit: Optional[str]) -> Optional[str]:
    """Read-only: curl blue's own /identity over the existing sshm helper
    (sourced verbatim, never re-typed). /identity is unauthenticated and
    200s with no side effect -- the same path manifest/apps.yaml's Plex
    health probe uses."""
    if explicit:
        return explicit
    port = _read_secret("plex.port", required=False) or "32400"
    curl = "curl -fsS --max-time 10 http://127.0.0.1:%s/identity" % shlex.quote(port)
    # Two homes (module docstring): from the workstation repo, blue is reached
    # through the sshm helper; ON the box (no repo tree around the script,
    # e.g. /tmp) blue IS localhost, so run the curl directly.
    try:
        ssh_lib = _HERE.parents[1] / "lib" / "ssh.sh"
    except IndexError:
        ssh_lib = None
    if ssh_lib is not None and ssh_lib.is_file():
        cmd = "source %s && sshm %s" % (shlex.quote(str(ssh_lib)), shlex.quote(curl))
    else:
        cmd = curl
    try:
        proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        print("could not reach blue to read its machineIdentifier (%s). "
              "Pass --old-machine-id explicitly." % e, file=sys.stderr)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        print("blue /identity probe failed (rc=%d): %s" % (proc.returncode, proc.stderr.strip()),
              file=sys.stderr)
        return None
    try:
        return ET.fromstring(proc.stdout).get("machineIdentifier")
    except ET.ParseError as e:
        print("blue /identity response was not valid XML: %s" % e, file=sys.stderr)
        return None

def resolve_green_machine_id(account, old_machine_id: str, explicit: Optional[str]):
    """(machine_id_or_None, note). No SSH, no NEW_HOST -- see module docstring."""
    if explicit:
        return explicit, "explicit --new-machine-id"
    servers = [r for r in account.resources()
               if getattr(r, "owned", False) and "server" in (getattr(r, "provides", "") or "").split(",")]
    candidates = [r for r in servers if r.clientIdentifier != old_machine_id]
    if not candidates:
        return None, "no other Plex Media Server owned by this account yet (green not claimed?)"
    if len(candidates) > 1:
        names = ", ".join("%s(%s)" % (r.name, r.clientIdentifier) for r in candidates)
        return None, "ambiguous: %d non-blue servers on this account [%s] -- pass --new-machine-id" % (
            len(candidates), names)
    return candidates[0].clientIdentifier, "resolved by elimination (%s)" % candidates[0].name

def classify_share(share, welcome_title: str):
    """(kind, titles_or_None, detail); kind in {full, welcome_only, anomalous}.
    Mirrors lib/plexshare.py's disjoint design (entitled = everything but
    Welcome; not entitled = Welcome alone) -- anything else is a state the
    gate itself never writes, so it is named and skipped, never guessed."""
    wt = welcome_title.strip().lower()
    if getattr(share, "allLibraries", False):
        return "full", None, "allLibraries=1 (legacy full grant, predates per-section shares)"
    titles = {s.title.strip() for s in share.sections()}
    lower = {t.lower() for t in titles}
    if lower == {wt}:
        return "welcome_only", None, "shares exactly the Welcome section"
    if not lower:
        return "anomalous", None, "share has zero sections -- cannot classify, skipping"
    if wt in lower:
        return "anomalous", None, "shares Welcome PLUS other sections (gate states should be disjoint)"
    return "full", sorted(titles), "shares %d non-Welcome section(s)" % len(titles)

def derive_full_titles(rows: List[Dict], welcome_title: str, override_csv: Optional[str]):
    if override_csv:
        return [t.strip() for t in override_csv.split(",") if t.strip()]
    for row in rows:
        if row["kind"] == "full" and row["titles"]:
            return [t for t in row["titles"] if t.strip().lower() != welcome_title.strip().lower()]
    return None

def existing_green_share(user, green_machine_id: str):
    for s in getattr(user, "servers", None) or []:
        if getattr(s, "machineIdentifier", None) == green_machine_id:
            return s
    return None

def build_plan(account, old_machine_id: str, welcome_title: str) -> List[Dict]:
    rows: List[Dict] = []
    for user in account.users():
        share = next((s for s in (getattr(user, "servers", None) or [])
                      if getattr(s, "machineIdentifier", None) == old_machine_id), None)
        if share is None:
            continue  # not a friend of blue -- out of scope
        kind, titles, detail = classify_share(share, welcome_title)
        email = user.email or user.username or ("id:%s" % user.id)
        rows.append({"user": user, "email": email, "masked": _mask_email(email),
                     "kind": kind, "titles": titles, "detail": detail})
    return rows

def print_plan(rows: List[Dict], full_titles, green_id: Optional[str], green_note: str,
               welcome_title: str) -> None:
    print("=== 45-plex-invites: dry-run plan ===")
    print("green machineIdentifier: %s (%s)" % (green_id or "UNRESOLVED", green_note))
    print("full-access set (%d titles): %s"
          % (len(full_titles or []), ", ".join(full_titles) if full_titles else "UNKNOWN -- pass --full-sections"))
    print("welcome-only set: [%s]\n" % welcome_title)
    for r in rows:
        want = ("[%s]" % welcome_title) if r["kind"] == "welcome_only" else (
            (", ".join(full_titles) if full_titles else "UNKNOWN") if r["kind"] == "full" else "SKIP")
        print("  %-28s kind=%-13s -> %s" % (r["masked"], r["kind"], want))
        print("        %s" % r["detail"])
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    print("\n%d blue friend(s): %s" % (len(rows), counts))

def execute_plan(account, rows: List[Dict], green_id: str, full_titles, welcome_title: str) -> int:
    ok = fail = skipped = 0
    for r in rows:
        if r["kind"] == "anomalous":
            print("SKIP  %s: %s" % (r["masked"], r["detail"]))
            skipped += 1
            continue
        desired = [welcome_title] if r["kind"] == "welcome_only" else full_titles
        if not desired:
            print("SKIP  %s: full-access set unresolved, nothing to invite with" % r["masked"])
            skipped += 1
            continue
        existing = existing_green_share(r["user"], green_id)
        try:
            if existing is None:
                account.inviteFriend(user=r["email"], server=green_id, sections=desired)
                print("INVITE %s -> %s" % (r["masked"], r["kind"]))
            else:
                have = {s.title.lower() for s in existing.sections()}
                if have == {t.lower() for t in desired}:
                    print("SKIP  %s: already shares the desired sections on green" % r["masked"])
                    skipped += 1
                    continue
                account.updateFriend(user=r["email"], server=green_id, sections=desired)
                print("UPDATE %s -> %s" % (r["masked"], r["kind"]))
            ok += 1
        except Exception as e:  # one bad share must not abort the rest of the run (I-4)
            print("FAIL  %s: %s" % (r["masked"], e), file=sys.stderr)
            fail += 1
    print("\ndone: %d ok, %d failed, %d skipped" % (ok, fail, skipped))
    return EXIT_OK if fail == 0 and skipped == 0 else EXIT_PARTIAL

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("new_host", nargs="?", default=None,
                     help="accepted for CLI parity with the other migrate/ scripts; unused (see docstring)")
    ap.add_argument("--execute", action="store_true", help="perform invites/updates (default: print plan only)")
    ap.add_argument("--old-machine-id", default=None, help="skip the read-only blue /identity probe")
    ap.add_argument("--new-machine-id", default=None, help="skip plex.tv resource elimination")
    ap.add_argument("--full-sections", default=None, help="CSV override for the full-access title set")
    ap.add_argument("--welcome-section", default=DEFAULT_WELCOME_TITLE)
    args = ap.parse_args()
    try:
        from plexapi.myplex import MyPlexAccount
    except ImportError:
        print("plexapi not importable -- `pip install plexapi` locally, or run via the "
              "box's venv (see 'RUNS TWO WAYS' in this file's docstring).", file=sys.stderr)
        return EXIT_CANNOT_ASSERT

    token = _read_secret("plex.token")
    old_id = discover_blue_machine_id(args.old_machine_id)
    if not old_id:
        print("could not determine blue's machineIdentifier.", file=sys.stderr)
        return EXIT_CANNOT_ASSERT
    try:
        account = MyPlexAccount(token=token)
    except Exception as e:
        print("could not authenticate to plex.tv: %s" % e, file=sys.stderr)
        return EXIT_CANNOT_ASSERT

    green_id, green_note = resolve_green_machine_id(account, old_id, args.new_machine_id)
    rows = build_plan(account, old_id, args.welcome_section)
    full_titles = derive_full_titles(rows, args.welcome_section, args.full_sections)
    if not args.execute:
        print_plan(rows, full_titles, green_id, green_note, args.welcome_section)
        return EXIT_OK

    if not green_id:
        print("--execute requires a resolved green machineIdentifier (%s)." % green_note, file=sys.stderr)
        return EXIT_CANNOT_ASSERT
    if not full_titles:
        print("--execute requires a full-access title set; none derivable and no --full-sections given.",
              file=sys.stderr)
        return EXIT_CANNOT_ASSERT
    return execute_plan(account, rows, green_id, full_titles, args.welcome_section)

if __name__ == "__main__":
    sys.exit(main())

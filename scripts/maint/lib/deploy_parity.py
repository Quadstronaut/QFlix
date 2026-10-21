#!/usr/bin/env python3
"""lib/deploy_parity.py - deployed manifest / appctl / units vs the commit (QFLX-20, spec 5.8).

WHY THIS EXISTS: the deploy-drift canary hashes only `*.py` and `*.sh` under
~/scripts. The manifest that drives a UCC->native flip is deployed FLAT to
~/.opt/maint/apps.yaml, `~/bin/appctl` lives outside ~/scripts, and units live in
~/.config/systemd/user. So "deploy-drift green" held even when a flip (or its
revert) was never deployed. This module closes that gap by comparing, byte for
byte, against the files at the comparison ref:

  manifest/apps.yaml     <-> ~/.opt/maint/apps.yaml
  manifest/jobs.yaml     <-> ~/.opt/maint/jobs.yaml
  scripts/lib/appctl     <-> ~/bin/appctl            (+ exec bit when git says 755)
  scripts/maint/systemd/<unit>
                         <-> ~/.config/systemd/user/<unit>
                             for every deployed manitoba-maint-* / qflix-* unit
                             that ALSO exists in git (deployed-only units are
                             audit-live's L-02; drop-in dirs are designed drift)

A missing deployed copy of the three fixed files is drift (it means the install
never ran), a missing unit is not (not every unit is deployed).

deploy-drift.sh feeds this module from the git object store
(`git show $REF:scripts/maint/lib/deploy_parity.py | python3 - ...`), so the
check always runs the commit's own logic and needs no separate deploy step.

CLI: deploy_parity.py --src GITDIR --ref REF [--home DIR]
  exit 0 clean (one PASS line on stdout), 1 drift (one `STAGE=` line on stderr),
  2 cannot compare.

SHIPPED CONFIGURE SCRIPTS (QFLX-52)
Most of scripts/configure/ is run FROM THE WORKSTATION and never belongs on the
box. Hand-copied ones did land there (80-vlogs, 90-sabnzbd-usenet, 91-nginx),
and deploy-drift then went red on every repo edit to them -- drift no installer
could clear. The rule is now derived, not listed: a deployed
~/scripts/configure/<name> is compared to git only if some installer at the
ref copies a file TO that path; otherwise it is "deployed but not shipped",
its own red (deploy-unshipped). A hand list of exclusions would have hidden
the next hand copy; deriving from the installers names it instead.

  deploy_parity.py --src GITDIR --ref REF --shipped-configure
    prints the shipped basenames one per line (exit 0), or exit 2 if none
    were found (a parser that finds nothing must never read as "ship nothing").
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

UNIT_PREFIXES = ("manitoba-maint-", "qflix-")
UNIT_SUFFIXES = (".service", ".timer", ".socket")

# deploy-drift.sh's walk, mirrored so tests derive the covered set from the SAME
# rule (its `find -name` globs and its `case` skip list). Change both together;
# test_vlogs_ingester_deps_staged pins the shell side to these.
DRIFT_SUFFIXES = (".py", ".sh")
DRIFT_SKIP_PARTS = (".venv", "site-packages", "node_modules", "__pycache__")

# Installer trees whose copy lines decide what lands in ~/scripts/configure.
INSTALLER_DIRS = ("scripts/configure", "scripts/install")
# An installer line that copies TO ~/scripts/configure/<name>. Anchored on the
# command (cp/install, after optional indent) so `rm -f ~/scripts/configure/x`
# decommission lines and comments never count as shipping. The destination is
# the LAST token: `cp -f "$STG"/scripts/configure/x ~/scripts/configure/x`. A
# copy to the bare directory is not matched -- that fails toward red
# (deploy-unshipped), never toward a silent pass.
_SHIP_RE = re.compile(
    r"^\s*(?:cp|install)\s.*\s(?:~|\$HOME|\$\{HOME\})/scripts/configure/"
    r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:#.*)?$", re.M)


def _git(src: str, *args: str) -> bytes | None:
    cp = subprocess.run(["git", "-C", src, *args], capture_output=True)
    return cp.stdout if cp.returncode == 0 else None


def _git_mode(src: str, ref: str, path: str) -> str | None:
    out = _git(src, "ls-tree", ref, "--", path)
    return out.decode().split()[0] if out else None


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def in_drift_scope(rel: str) -> bool:
    """True when deploy-drift's walk would look at ~/scripts/<rel> at all."""
    parts = rel.split("/")
    return rel.endswith(DRIFT_SUFFIXES) and not any(p in DRIFT_SKIP_PARTS for p in parts[:-1])


def shipped_configure_from_text(text: str) -> set[str]:
    """Basenames some installer copies into ~/scripts/configure/."""
    return set(_SHIP_RE.findall(text))


def shipped_configure(src: str, ref: str) -> set[str]:
    """shipped_configure_from_text over every installer AT THE REF. One
    `git grep` (cheap pre-filter; the Python regex is the real rule) so the
    canary reads the commit under test, not whatever is checked out."""
    out = _git(src, "grep", "-h", "-I", "-E",
               r"^[[:space:]]*(cp|install)[[:space:]].*/scripts/configure/",
               ref, "--", *INSTALLER_DIRS)
    # git grep exits 1 on "no match": that is an empty set, which the CLI
    # refuses below. Only a broken ref is an error here.
    if out is None and _git(src, "rev-parse", "--verify", ref + "^{commit}") is None:
        raise RuntimeError(f"ref {ref} not resolvable in {src}")
    return shipped_configure_from_text((out or b"").decode("utf-8", "replace"))


def compare(src: str, ref: str, home: Path) -> dict:
    """Return {"drift": [...], "match": int}. Raises RuntimeError if git cannot
    answer at all (a stale or absent ref must never read as clean)."""
    if _git(src, "rev-parse", "--verify", ref + "^{commit}") is None:
        raise RuntimeError(f"ref {ref} not resolvable in {src}")
    drift: list[str] = []
    match = 0

    fixed = [
        ("manifest/apps.yaml", home / ".opt" / "maint" / "apps.yaml"),
        ("manifest/jobs.yaml", home / ".opt" / "maint" / "jobs.yaml"),
        ("scripts/lib/appctl", home / "bin" / "appctl"),
    ]
    units_dir = home / ".config" / "systemd" / "user"
    if units_dir.is_dir():
        for p in sorted(units_dir.iterdir()):
            if (p.is_file() and p.name.startswith(UNIT_PREFIXES)
                    and p.name.endswith(UNIT_SUFFIXES)):
                fixed.append((f"scripts/maint/systemd/{p.name}", p))

    for gitpath, deployed in fixed:
        is_unit = gitpath.startswith("scripts/maint/systemd/")
        want = _git(src, "show", f"{ref}:{gitpath}")
        if want is None:
            if is_unit:
                continue                       # deployed-only unit: L-02's job
            raise RuntimeError(f"{gitpath} not in {ref}")
        try:
            have = deployed.read_bytes()
        except OSError:
            drift.append(f"{gitpath}:missing")
            continue
        if _md5(have) != _md5(want):
            drift.append(f"{gitpath}:differs")
            continue
        if gitpath == "scripts/lib/appctl" and _git_mode(src, ref, gitpath) == "100755" \
                and not os.access(deployed, os.X_OK):
            drift.append(f"{gitpath}:not-executable")
            continue
        match += 1
    return {"drift": drift, "match": match}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--src", required=True)
    ap.add_argument("--ref", required=True)
    ap.add_argument("--home", default=str(Path.home()))
    ap.add_argument("--shipped-configure", action="store_true",
                    help="print configure/ basenames an installer ships, then exit")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.shipped_configure:
        try:
            names = shipped_configure(args.src, args.ref)
        except Exception as exc:                               # noqa: BLE001
            print(f"STAGE=deploy-parity-error msg={str(exc)[:120].replace(' ', '-')}",
                  file=sys.stderr)
            return 2
        if not names:
            # 240 ships itself, so empty means the parser broke -- never "none".
            print("STAGE=deploy-parity-error msg=no-shipped-configure-scripts-found-parser-broken",
                  file=sys.stderr)
            return 2
        print("\n".join(sorted(names)))
        return 0
    try:
        res = compare(args.src, args.ref, Path(args.home))
    except Exception as exc:                                   # noqa: BLE001
        print(f"STAGE=deploy-parity-error msg={str(exc)[:120].replace(' ', '-')}",
              file=sys.stderr)
        return 2
    if res["drift"]:
        shown = " ".join(res["drift"])[:200]
        print(f"STAGE=deploy-parity-drift msg={len(res['drift'])}-deployed-manifest-appctl-or-unit-"
              f"files-differ-from-{args.ref} files={shown}", file=sys.stderr)
        return 1
    print(f"PASS: deploy-parity - {res['match']} manifest/appctl/unit files match {args.ref}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

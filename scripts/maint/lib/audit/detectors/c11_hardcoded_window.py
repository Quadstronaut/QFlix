"""C-11 hard-coded-maintenance-window.

QFLX-16 (O-5). The Monday 11:00-15:00 UTC window used to be re-implemented in
nine files as `now.weekday() == 0 and 11 <= now.hour < 15` (or `date +%u`).
That made "the window may be none on a generic host" impossible to honour and
let the copies drift. The window now lives in lib/hostpolicy*.py only; every
caller asks `in_maintenance_window(now)`.

The rule: a Monday-shaped literal (`weekday() == 0`, `isoweekday() == 1`,
`date +%u`, `date +%a`) in a tracked script is a finding UNLESS
  * the file is a hostpolicy module (the one legitimate home), or
  * the line (or the one above it) carries `window-ok: <reason>`, the
    adjudication that this Monday check is a CADENCE (a weekly send, a digest),
    not the maintenance window.

ENFORCED: the migration is complete, so a new literal is a regression, and the
adjudication marker keeps the false-positive cost to one comment.
"""
from __future__ import annotations

import re
from typing import List

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c11_hardcoded_window"
CLASS_ID = "C-11"
BOUNDARY = "Monday-shaped weekday literals in tracked scripts (maint, canaries, mcp, lib)"

SCAN_GLOBS = [
    "scripts/maint/*.py",
    "scripts/maint/lib/*.py",
    "scripts/canaries/*.sh",
    "scripts/mcp/*.py",
    "scripts/lib/*.sh",
]

POLICY_HOMES = ("scripts/maint/lib/hostpolicy.py",
                "scripts/maint/lib/hostpolicy_ultra.py",
                "scripts/maint/lib/hostpolicy_generic.py")

MONDAY_LITERAL = re.compile(
    r"\bweekday\(\)\s*==\s*0\b|\bisoweekday\(\)\s*==\s*1\b|\+%u\b|\+%a\b")
ADJUDICATED = re.compile(r"window-ok:\s*\S")


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    files = repo.tracked_matching(SCAN_GLOBS)
    verdicts: List[Verdict] = []
    sites = 0
    for path in files:
        lines = repo.read(path).split("\n")
        for i, line in enumerate(lines, start=1):
            if line.strip().startswith("#"):
                continue
            if not MONDAY_LITERAL.search(line):
                continue
            sites += 1
            iid = path + ":" + str(i) + ":monday"
            prev = lines[i - 2] if i >= 2 else ""
            if path in POLICY_HOMES:
                verdicts.append(Verdict(iid, "policy-module", OK, path, i,
                                        "the host policy is the one home for the window"))
            elif ADJUDICATED.search(line) or ADJUDICATED.search(prev):
                verdicts.append(Verdict(iid, "adjudicated-cadence", OK, path, i,
                                        "window-ok marker: a cadence, not the maintenance window"))
            else:
                verdicts.append(Verdict(
                    iid, "hardcoded-monday-window", FINDING, path, i,
                    "hard-coded Monday check; call hostpolicy.in_maintenance_window(now) "
                    "(or add `window-ok: <reason>` if this is a cadence, not the window)",
                ))
    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=sites,
        verdicts=verdicts,
        metrics={"files_scanned": len(files), "monday_literal_sites": sites},
    )

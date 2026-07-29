"""C-09 silent-exit-on-missing-prerequisite.

Memory kuma-monitors-born-mute, generalised: "canary push tokens live at
~/secrets/ NOT ~/.opt/maint/ and a missing token silently exits 0". A canary
that cannot find its token exits clean, Kuma sees a green push it never got,
and the check has been dead for weeks before anyone notices. REA's five
early-return deadman paths are the same shape on the other host.

The rule: an `exit 0` / `return 0` / `sys.exit(0)` reached from a guard that
just discovered a MISSING prerequisite (a secret, a token, a binary) is a
finding. An `exit 0` that ends a successful path, or that skips work for a
DECLARED reason (a maintenance window, a suppression registry, a lock), is not.

ADVISORY on landing and it will stay advisory until adjudicated: telling those
two apart from three lines of context has a real false-positive rate, and a
noisy enforced class is how a regime gets ignored.
"""
from __future__ import annotations

import re
from typing import List

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c09_silent_exit"
CLASS_ID = "C-09"
BOUNDARY = "clean-exit statements in tracked scripts/canaries/*.sh and self-pushing job scripts"

SCAN_GLOBS = [
    "scripts/canaries/*.sh",
    "scripts/maint/*.py",
    "scripts/maint/*.sh",
    "scripts/mcp/*.py",
]

# Leading whitespace is normal (guards live inside if/case blocks), so `^\s*`
# rather than `^`. The first cut used a bare `^` and found 16 sites where the
# repo has 30 — a detector that under-counts its own boundary is exactly the
# silent-omission bug this regime exists to kill, so it is called out here.
CLEAN_EXIT = re.compile(r"(^\s*|[;&|]\s*|\bthen\s+|\)\s+)(exit\s+0\b|sys\.exit\(0\)|return\s+0\b)")

# The guard vocabulary that means "a thing we needed is not there".
MISSING_PREREQ = re.compile(
    r"(?i)(\[\s*!\s*-[frxse]\s|\bif\s+not\s+.*\b(exists|is_file|token|secret|key)\b|"
    r"command\s+-v\b|\bwhich\b|not\s+found|missing|no\s+(token|key|secret|binary)|"
    r"-z\s+\"?\$)"
)
# ...and the vocabulary that means "we are skipping ON PURPOSE".
DECLARED_SKIP = re.compile(
    r"(?i)(window|suppress|paused?|lock|maintenance|dry.?run|disabled|opt.?out|"
    r"quiet.?hours|nothing to do|already|no-op|skip(ping)? by design)"
)
PREREQ_WORDS = re.compile(r"(?i)(token|secret|api.?key|credential|command -v|which |binary|executable)")


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    files = repo.tracked_matching(SCAN_GLOBS)
    verdicts: List[Verdict] = []
    sites = 0
    # Sub-counts, so "0 findings" is auditable rather than merely reassuring.
    # A detector that reports nothing because its first filter never fires
    # looks identical to a clean repo — these numbers tell them apart.
    guarded = 0
    declared_skips = 0

    for path in files:
        lines = repo.read(path).split("\n")
        for i, line in enumerate(lines, start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if not CLEAN_EXIT.search(line):
                continue
            sites += 1
            window = "\n".join(lines[max(0, i - 5):i])
            iid = path + ":" + str(i) + ":exit0"
            if not MISSING_PREREQ.search(window):
                verdicts.append(Verdict(iid, "exit-not-a-guard", OK, path, i,
                                        "clean exit with no missing-prerequisite guard above it"))
                continue
            guarded += 1
            if DECLARED_SKIP.search(window):
                declared_skips += 1
                verdicts.append(Verdict(iid, "declared-skip", OK, path, i,
                                        "guard is a declared skip (window/suppression/lock), not a missing prerequisite"))
                continue
            if not PREREQ_WORDS.search(window):
                verdicts.append(Verdict(iid, "guard-not-prerequisite", OK, path, i,
                                        "guard does not test for a secret/token/binary"))
                continue
            verdicts.append(Verdict(
                iid, "guard-exits-clean", FINDING, path, i,
                "a missing prerequisite (secret/token/binary) leads to a CLEAN exit: "
                "the check reports success it never performed",
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=sites,
        verdicts=verdicts,
        metrics={
            "files_scanned": len(files),
            "clean_exit_sites": sites,
            "sites_behind_a_guard": guarded,
            "sites_declared_skip": declared_skips,
        },
    )

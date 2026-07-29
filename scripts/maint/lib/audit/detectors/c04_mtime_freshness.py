"""C-04 mtime-freshness.

Generalised from cbde5c1 ("Freshness was gated on FILE mtime, not LINE
timestamp"). A file's mtime says when it was last WRITTEN, which is not when
its newest CONTENT is from: a log that is rotated, touched, or rewritten with
old data looks fresh forever. Anywhere a staleness DECISION rides on mtime, the
ledger must carry a written reason saying why the file clock is the right clock
there.

Boundary is textual rather than AST because the same mistake is made in bash
(`stat -c %Y`, `find -newermt`) and PowerShell (`LastWriteTime`) as in Python,
and a Python-only detector would have declared the class closed while two
thirds of the surface went unlooked-at.
"""
from __future__ import annotations

import re
from typing import List

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c04_mtime_freshness"
CLASS_ID = "C-04"
BOUNDARY = "mtime accessors in tracked scripts/**, scored for freshness context"

SCAN_GLOBS = ["scripts/**"]
BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".zip", ".gz")

ACCESSOR = re.compile(
    r"st_mtime|getmtime|\bmtime\b|LastWriteTime|stat\s+-c\s*['\"]?%Y|-newermt|"
    r"\.stat\(\)\.st_mtime"
)
# A freshness DECISION nearby: the identifier vocabulary that means "we are
# about to judge whether this is recent enough".
FRESHNESS = re.compile(r"(?i)\b\w*(stale|fresh|age|recent|expir|cutoff|older|newer)\w*\b")
# Lines that are only doing housekeeping (deleting old files by age) are the
# legitimate use: there the file clock IS the subject, not a proxy for content.
HOUSEKEEPING = re.compile(r"(?i)(unlink|remove|delete|prune|rotate|cleanup|retention|rm\s)")


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    files = [
        p for p in repo.tracked_matching(SCAN_GLOBS)
        if not p.lower().endswith(BINARY_SUFFIXES)
    ]
    verdicts: List[Verdict] = []
    refs = 0
    in_context = 0

    for path in files:
        text = repo.read(path)
        if not ACCESSOR.search(text):
            continue
        lines = text.split("\n")
        for i, line in enumerate(lines, start=1):
            if not ACCESSOR.search(line):
                continue
            refs += 1
            window = "\n".join(lines[max(0, i - 6):i + 5])
            iid = path + ":" + str(i) + ":mtime"
            if not FRESHNESS.search(window):
                verdicts.append(Verdict(iid, "mtime-not-freshness", OK, path, i,
                                        "mtime reference with no staleness vocabulary nearby"))
                continue
            in_context += 1
            if HOUSEKEEPING.search(window):
                verdicts.append(Verdict(iid, "mtime-housekeeping", OK, path, i,
                                        "age-based file housekeeping: the file clock is the subject"))
                continue
            verdicts.append(Verdict(
                iid, "freshness-from-mtime", FINDING, path, i,
                "staleness decided from file mtime, not from content timestamps",
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=refs,
        verdicts=verdicts,
        metrics={
            "files_scanned": len(files),
            "mtime_references": refs,
            "in_freshness_context": in_context,
        },
    )

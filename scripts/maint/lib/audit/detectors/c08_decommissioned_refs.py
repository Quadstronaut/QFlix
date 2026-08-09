"""C-08 decommissioned-still-referenced.

Every decommission in this repo left references behind: maintainerr in 19
files, homarr in 9, quadstronix in 3 (measured over scripts/ + manifest/ at
HEAD 06d4226). Some are correct — a changelog that stopped naming what it
removed would be a worse artifact. Some are live code paths pointing at
software that no longer exists.

The detector enumerates every (component, file, line) occurrence and puts each
into exactly one bucket:
  history-surface   the file is a declared history surface (changelog,
                    transition log, incidents, inventory, dated audit docs)
  declared-retired  the LINE itself says retired/decommissioned/replaced-by
  adjudicated       an allow_paths entry with a written reason
  live-reference    everything else -> finding

Counts are RE-DERIVED here, never asserted as literals: the point is that the
detector reproduces the baseline, not that somebody typed 19 into a test.
"""
from __future__ import annotations

import re
from typing import Dict, List

from ..model import FINDING, OK, DetectorResult, Verdict
from ..repo import glob_match

NAME = "c08_decommissioned_refs"
CLASS_ID = "C-08"
BOUNDARY = "manifest/decommissioned.yaml components x every tracked text file"

BINARY_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2",
                   ".zip", ".gz", ".pdf", ".jar", ".keystore")


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    led = ctx.ledgers.decommissioned
    components = led.get("components") or []
    history = [h.get("path") for h in (led.get("allowed_context_paths") or [])]
    line_rules = [
        re.compile(p["pattern"]) for p in (led.get("allowed_line_patterns") or [])
        if p.get("pattern")
    ]

    files = [f for f in repo.tracked if not f.lower().endswith(BINARY_SUFFIXES)]
    verdicts: List[Verdict] = []
    occurrences = 0
    per_component: Dict[str, int] = {}
    per_component_files: Dict[str, int] = {}

    for comp in sorted(components, key=lambda c: c["id"]):
        cid = comp["id"]
        rx = re.compile("|".join(re.escape(n) for n in comp.get("names") or [cid]), re.I)
        allow = {a["path"]: a for a in (comp.get("allow_paths") or [])}
        per_component[cid] = 0
        per_component_files[cid] = 0

        for path in files:
            text = repo.read(path)
            if not rx.search(text):
                continue
            per_component_files[cid] += 1
            in_history = any(glob_match(h, path) for h in history if h)
            allowed_here = next(
                (a for p, a in allow.items() if glob_match(p, path)), None)

            for lineno, line in enumerate(text.split("\n"), start=1):
                if not rx.search(line):
                    continue
                occurrences += 1
                per_component[cid] += 1
                iid = path + ":" + str(lineno) + ":" + cid
                if in_history:
                    verdicts.append(Verdict(iid, "history-surface", OK, path, lineno,
                                            "declared history surface"))
                elif allowed_here is not None:
                    verdicts.append(Verdict(iid, "adjudicated", OK, path, lineno,
                                            "allow_paths adjudication for this file"))
                elif any(r.search(line) for r in line_rules):
                    verdicts.append(Verdict(iid, "declared-retired", OK, path, lineno,
                                            "the line itself declares the retirement"))
                else:
                    verdicts.append(Verdict(
                        iid, "live-reference", FINDING, path, lineno,
                        "'" + cid + "' was retired " + str(comp.get("retired"))
                        + " but this line references it as if it were live",
                    ))

    metrics = {"components": len(components), "files_scanned": len(files),
               "occurrences": occurrences}
    for cid, n in sorted(per_component.items()):
        metrics["occurrences_" + cid] = n
        metrics["files_" + cid] = per_component_files[cid]

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=occurrences,
        verdicts=verdicts,
        metrics=metrics,
    )

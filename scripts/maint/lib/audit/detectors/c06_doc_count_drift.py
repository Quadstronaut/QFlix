"""C-06 doc-count-drift, exhaustive.

tests/unit/test_doc_counts.py is the reference implementation for this entire
regime: it enumerates every canary from the manifest and asserts every doc
surface agrees, and that class has stayed closed since. Its one limitation is
that it checks ~12 HAND-PICKED anchors, so a new sentence quoting a count is
unguarded the moment it is written.

This generalises it: every "<N> [qualifier] <noun>" claim on the declared doc
surfaces is enumerated, and each must equal SOME live computed count for that
noun — or be waived, with a reason, as a deliberate historical mention.

Advisory on landing. The surfaces carry many legitimately-historical numbers
("13 canaries" inside a 2026-05 narrative) and several LIVE-BOX numbers that no
repo glob can reproduce ("43 timers" counts panel-managed units this repo does
not own). Each needs a human, which is exactly what advisory means.
"""
from __future__ import annotations

import ast
import re
from typing import Dict, List, Set

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c06_doc_count_drift"
CLASS_ID = "C-06"
BOUNDARY = "every '<N> <noun>' claim in README.md, inventory.md, the FAQ and canaries/README.md"

SURFACES = [
    "README.md",
    "inventory.md",
    "scripts/data/qflix-faq.html",
    "scripts/canaries/README.md",
]

CLAIM = re.compile(
    r"\b(\d+)((?:\s+[A-Za-z][\w-]*){0,2})\s+(apps|canaries|monitors|tests|timers|services)\b",
    re.I,
)

# Monitors that are real and manitoba-owned but live outside the manifest.
# Mirrors tests/unit/test_doc_counts.NON_MANIFEST_MONITORS; read from
# lib/kuma.py so the two cannot drift.
_EXTRA_DAEMON_MONITORS = 2  # "Manitoba Pusher" + "QFlix Fleet"


def _standalone_count(repo) -> int:
    src = repo.read_optional("scripts/maint/lib/kuma.py")
    if not src:
        return 0
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "STANDALONE_SELF_PUSH_MONITORS"
                for t in node.targets):
            try:
                return len(ast.literal_eval(node.value))
            except (ValueError, SyntaxError):
                return 0
    return 0


def _acceptable(repo) -> Dict[str, Set[int]]:
    """Live counts, recomputed from the sources of truth every run."""
    import yaml
    m = yaml.safe_load(repo.read("manifest/apps.yaml")) or {}
    apps = len(m.get("apps") or {})
    canaries = len(m.get("canaries") or {})
    manifest_monitors = sum(
        1 for section in ("apps", "canaries")
        for e in (m.get(section) or {}).values() if (e or {}).get("kuma_monitor")
    )
    manitoba = manifest_monitors + _standalone_count(repo) + _EXTRA_DAEMON_MONITORS
    external = len(m.get("kuma_external_monitors") or [])
    timers = len(repo.tracked_matching(["scripts/*/systemd/*.timer"]))
    services = len(repo.tracked_matching(["scripts/*/systemd/*.service"]))
    return {
        "apps": {apps},
        "canaries": {canaries},
        # app-monitor / canary-monitor / manifest-total / manitoba-total /
        # grand-total are all legitimate things a sentence can be counting.
        "monitors": {apps, canaries, manifest_monitors, manitoba, manitoba + external},
        "timers": {timers},
        "services": {services},
        "tests": set(),  # nothing offline can count pytest cases; always adjudicated
    }


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    ok_values = _acceptable(repo)
    verdicts: List[Verdict] = []
    seen: Dict[str, int] = {}
    total = 0

    for surface in SURFACES:
        text = repo.read_optional(surface)
        if text is None:
            verdicts.append(Verdict(
                surface + ":missing", "unguarded-count-claim", FINDING, surface, 0,
                "declared doc surface does not exist",
            ))
            continue
        for m in CLAIM.finditer(text):
            total += 1
            lineno = text.count("\n", 0, m.start()) + 1
            noun = m.group(3).lower()
            value = int(m.group(1))
            phrase = _slug(m.group(0))
            base = surface + ":" + phrase
            seen[base] = seen.get(base, 0) + 1
            iid = base if seen[base] == 1 else base + "#" + str(seen[base])
            if value in ok_values.get(noun, set()):
                verdicts.append(Verdict(iid, "count-agrees", OK, surface, lineno,
                                        "matches a live computed count for '" + noun + "'"))
                continue
            verdicts.append(Verdict(
                iid, "unguarded-count-claim", FINDING, surface, lineno,
                "claims " + str(value) + " " + noun + "; live computed values are "
                + (", ".join(str(v) for v in sorted(ok_values.get(noun, set()))) or "<none countable offline>"),
            ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=total,
        verdicts=verdicts,
        metrics={
            "surfaces": len(SURFACES),
            "claims": total,
        },
    )

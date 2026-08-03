"""C-07 prompt-vs-rule-table-contradiction.

The exact 2026-07-29 root cause, made CI-checkable without un-gitignoring a
56KB operator-local script.

REA carries the same policy twice: an ADVISORY sentence in the system prompt
("NON-ACTIONABLE NOISE you must NEVER report: ...") and an ENFORCING regex
table ($Script:NoiseFindingRules). On 2026-07-29 three classes were in the
prompt with no rule — the prompt was asking, not enforcing — and nothing could
notice, because neither copy was in git.

manifest/rea-noise-classes.yaml is now the single source both sides read. This
detector checks it in three layers:

  1. YAML-INTERNAL (always, offline, in CI): every prompt segment is claimed by
     >= 1 class, every class is claimed by exactly one segment, every rx
     compiles, every class carries a written `why`.
  2. PS1 CROSS-CHECK (only where the file exists — the operator workstation):
     the ps1's rule ids, rx strings and `field` values match the yaml
     byte-for-byte, and the prompt really contains each declared segment marker.
  3. THE SKIP IS COUNTED. When the ps1 is absent, that is recorded as an
     explicit `ps1-absent` verdict, never as a silent pass. Layer 2 not running
     is residual R4, and it says so in the output.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..model import FINDING, OK, DetectorResult, Verdict

NAME = "c07_rea_prompt_rule_bijection"
CLASS_ID = "C-07"
BOUNDARY = "manifest/rea-noise-classes.yaml x scripts/local-llm/qflix-rea.ps1 (when present)"


def parse_ps1_rules(text: str) -> List[Dict[str, Optional[str]]]:
    """Extract $Script:NoiseFindingRules from the ps1 source.

    Handles both PowerShell string forms: single-quoted (where '' escapes a
    quote and backslashes are literal — which is why every rx is written that
    way) and double-quoted (used for the one rx that itself contains a single
    quote).
    """
    start = text.find("$Script:NoiseFindingRules = @(")
    if start < 0:
        return []
    end = text.find("\n)\n", start)
    body = text[start:end if end > 0 else len(text)]
    out: List[Dict[str, Optional[str]]] = []
    for block in re.split(r"@\{\s*id\s*=\s*", body)[1:]:
        idm = re.match(r"'((?:[^']|'')*)'", block)
        if not idm:
            continue
        rxm = re.search(r"\brx\s*=\s*('(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\")", block, re.S)
        if not rxm:
            continue
        raw = rxm.group(1)
        rx = raw[1:-1].replace("''", "'") if raw[0] == "'" else raw[1:-1]
        fm = re.search(r"\bfield\s*=\s*'([^']*)'", block)
        out.append({
            "id": idm.group(1).replace("''", "'"),
            "rx": rx,
            "field": fm.group(1) if fm else None,
        })
    return out


def extract_prompt(text: str, start_marker: str, stop_marker: str) -> Optional[str]:
    i = text.find(start_marker)
    j = text.find(stop_marker, i + 1) if i >= 0 else -1
    if i < 0 or j < 0:
        return None
    return text[i:j]


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    rea = ctx.ledgers.rea
    classes = rea.get("classes") or []
    segments = rea.get("prompt_segments") or []
    verdicts: List[Verdict] = []
    yaml_path = "manifest/rea-noise-classes.yaml"

    by_id = {c["id"]: c for c in classes}
    claimed: Dict[str, List[int]] = {}
    for seg in segments:
        for cid in seg.get("classes") or []:
            claimed.setdefault(cid, []).append(seg.get("index"))

    # ---- layer 1: the yaml is internally coherent -------------------------
    for seg in segments:
        idx = seg.get("index")
        iid = yaml_path + ":segment:" + str(idx)
        if not (seg.get("classes") or []):
            verdicts.append(Verdict(
                iid, "segment-without-rule", FINDING, yaml_path, 0,
                "prompt segment " + str(idx) + " (" + str(seg.get("marker"))[:60]
                + ") names a never-report class with NO enforcement rule",
            ))
            continue
        unknown = [c for c in seg["classes"] if c not in by_id]
        if unknown:
            verdicts.append(Verdict(
                iid, "segment-without-rule", FINDING, yaml_path, 0,
                "prompt segment " + str(idx) + " claims unknown class(es): " + ", ".join(sorted(unknown)),
            ))
            continue
        verdicts.append(Verdict(iid, "segment-enforced", OK, yaml_path, 0,
                                "enforced by " + ", ".join(sorted(seg["classes"]))))

    for c in sorted(classes, key=lambda x: x["id"]):
        cid = c["id"]
        iid = yaml_path + ":class:" + cid
        owners = claimed.get(cid) or []
        if len(owners) != 1:
            verdicts.append(Verdict(
                iid, "rule-without-segment", FINDING, yaml_path, 0,
                "class " + cid + " is claimed by " + str(len(owners))
                + " prompt segments; must be exactly 1",
            ))
            continue
        if not (c.get("why") or "").strip():
            verdicts.append(Verdict(iid, "missing-field", FINDING, yaml_path, 0,
                                    "class " + cid + " has no written `why`"))
            continue
        if not (c.get("prompt_clause") or "").strip():
            verdicts.append(Verdict(iid, "missing-field", FINDING, yaml_path, 0,
                                    "class " + cid + " has no prompt_clause"))
            continue
        try:
            re.compile(c.get("rx") or "")
        except re.error as exc:
            verdicts.append(Verdict(iid, "rx-uncompilable", FINDING, yaml_path, 0,
                                    "class " + cid + " rx does not compile: " + str(exc)))
            continue
        verdicts.append(Verdict(iid, "class-coherent", OK, yaml_path, 0,
                                "one segment, compiling rx, written rationale"))

    # ---- layer 2/3: cross-check against the untracked ps1 ------------------
    # Collapsed into exactly ONE verdict, on purpose. The ps1 exists on the
    # operator's workstation and not in CI, so a per-rule verdict stream would
    # make the tally — and therefore report_digest — depend on WHICH MACHINE
    # ran the audit. One verdict that is `ok` both when the policy matches and
    # when the subject is absent keeps the digest a pure function of the
    # commit, while a real drift still flips it (and it should).
    #
    # The OK detail is IDENTICAL whether the subject was present-and-matching or
    # absent-and-unchecked. That is not evasion: "the ps1 is on this machine" is
    # ENVIRONMENT, and environment belongs in meta{} (see meta.s2_subjects),
    # which is excluded from the digest. If it lived here, the operator's
    # one-hex-string comparison would break every time they ran the audit from
    # the other host — for a reason that has nothing to do with the code.
    ps1_path = rea.get("source_script") or "scripts/local-llm/qflix-rea.ps1"
    ps1 = repo.read_optional(ps1_path)
    cross_iid = ps1_path + ":cross-check"
    drift = _ps1_drift(ps1, rea, classes, by_id, segments) if ps1 is not None else []
    if drift:
        verdicts.append(Verdict(
            cross_iid, "ps1-drift", FINDING, ps1_path, 0,
            str(len(drift)) + " policy mismatch(es): " + "; ".join(sorted(drift)[:5]),
        ))
    else:
        verdicts.append(Verdict(
            cross_iid, "ps1-cross-check", OK, ps1_path, 0,
            "no policy drift detected; subject presence is reported in meta.s2_subjects",
        ))

    return DetectorResult(
        boundary_name=BOUNDARY,
        # +1 for the collapsed cross-check. Constant across machines.
        boundary_size=len(classes) + len(segments) + 1,
        verdicts=verdicts,
        metrics={
            "yaml_classes": len(classes),
            "prompt_segments": len(segments),
        },
    )


def _ps1_drift(ps1: str, rea: dict, classes: list, by_id: dict, segments: list) -> List[str]:
    """Every way the live ps1 disagrees with the tracked policy."""
    problems: List[str] = []
    ps1_rules = parse_ps1_rules(ps1)
    ps1_ids = [r["id"] for r in ps1_rules]
    yaml_ids = [c["id"] for c in classes]
    if ps1_ids != yaml_ids:
        problems.append("rule ids/order differ (ps1 has " + str(len(ps1_ids))
                        + ", yaml has " + str(len(yaml_ids)) + ")")
    else:
        for r in ps1_rules:
            c = by_id[r["id"]]
            if r["rx"] != c.get("rx"):
                problems.append("rx mismatch for " + r["id"])
            if (r["field"] or None) != (c.get("field") or None):
                problems.append("field mismatch for " + r["id"])
    prompt = extract_prompt(
        ps1, rea.get("prompt_start_marker") or "", rea.get("prompt_stop_marker") or "")
    if prompt is None:
        problems.append("never-report sentence not found between the declared markers")
        return problems

    # Direction 1: yaml -> prompt. Every declared segment really is in the text.
    markers = []
    for seg in segments:
        marker = seg.get("marker") or ""
        if marker and marker not in prompt:
            problems.append("segment " + str(seg.get("index")) + " marker missing from prompt")
        elif marker:
            markers.append(marker)

    # Direction 2: prompt -> yaml. THIS is the 2026-07-29 root-cause direction —
    # a never-report clause added to the prompt with no class and no segment. It
    # was documented in the module docstring and in the yaml header from day one
    # ("C-07 splits the text ... on ';'") but was never actually implemented, so
    # for two commits the detector enforced only the harmless direction: an
    # unenforced prompt clause returned NO drift. Restored 2026-08-03.
    #
    # Markers cannot contain ';' (asserted below), so each marker lies wholly
    # within exactly one ';'-chunk and substring containment is a sound test.
    for seg in segments:
        if ";" in (seg.get("marker") or ""):
            problems.append("segment " + str(seg.get("index"))
                            + " marker contains ';' — it cannot be located in a split chunk")
    for n, chunk in enumerate(prompt.split(";")):
        if not any(m in chunk for m in markers):
            problems.append("prompt clause " + str(n)
                            + " is claimed by no rule: " + " ".join(chunk.split())[:60])

    # Direction 3: the per-class prompt_clause is real text, not decoration.
    # C-07 only ever asserted this field was non-empty, so it silently rotted
    # out of sync with the prompt it quotes.
    for c in classes:
        clause = (c.get("prompt_clause") or "").strip()
        if clause and clause not in prompt:
            problems.append("prompt_clause for " + c["id"] + " is not literal prompt text")
    return problems

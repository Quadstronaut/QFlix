#!/usr/bin/env python3
"""qflix-audit — the deterministic, offline half of the Convergent Audit Regime.

    python3 scripts/maint/qflix-audit.py            # human summary
    python3 scripts/maint/qflix-audit.py --json     # machine report + digest

WHAT THIS ANSWERS
-----------------
Not "is the system clean" — nothing can answer that. It answers "did anything
change since the last time you looked", with one hex string:

    report_digest  sha256 over the canonical findings body. Same commit, same
                   digest. A finding can only be NEW when an input changed or a
                   class was newly enrolled, and both are attributable.

EXIT CODES — the distinction that matters most
    0  no enforced findings
    1  FINDINGS: at least one finding in an enforced class
    2  REGIME INTEGRITY: the auditor itself is broken (ledger/detector
       bijection, scope partition, waiver or residual discipline, digest
       hygiene). "The auditor is broken" must never look like "the auditor
       found nothing", so it never shares an exit code with either.

WHAT THIS DOES NOT DO — read docs/audit-residual-risk.md. R1..R6 remain. Live
box state (L-01..L-06) is out of reach by construction and belongs to
scripts/maint/qflix-audit-live.py.

Compartmentalisation (operator design law): this is its own module, its own
timer (manitoba-maint-audit.timer) and its own Kuma monitor ("QFlix Audit
Regime"). Nothing here is folded into functional-audit.py or qflix-collect.py.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.audit.engine import (EXIT_CLEAN, EXIT_FINDINGS, EXIT_REGIME,  # noqa: E402
                              exit_code_for, run)
from lib.audit.model import RegimeError  # noqa: E402
from lib.audit.repo import Repo, find_repo_root  # noqa: E402

KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
KUMA_PUSH_KEY = "qflix-audit"
KUMA_MONITOR = "QFlix Audit Regime"


def _log_dir() -> Path:
    return Path(os.environ.get(
        "QFLIX_AUDIT_LOG_DIR", str(Path.home() / ".opt" / "maint" / "audit")))


def _write_audit_log(lines, digest: str) -> None:
    """Durable log. Every waiver line lands here with its class + rule id —
    the second design law: nothing is suppressed silently.

    Failure to write is reported LOUDLY on stderr rather than swallowed: a
    janitor that cannot write its own audit trail is a fact the operator needs,
    and swallowing it would make this file an instance of C-03.
    """
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        d = _log_dir()
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "qflix-audit.log", "a", encoding="utf-8") as fh:
            fh.write(stamp + " digest=" + digest + "\n")
            for line in lines:
                fh.write(stamp + " " + line + "\n")
    except OSError as exc:
        print("[qflix-audit] WARNING: could not write the audit log: " + str(exc),
              file=sys.stderr)


def _push_kuma(status: str, msg: str) -> None:
    """Best-effort heartbeat. A MISSING TOKEN IS REPORTED, not silently
    skipped — that is the C-09 class this regime enumerates, and the auditor
    does not get to be an instance of it."""
    import urllib.parse
    import urllib.request
    token = os.environ.get("QFLIX_AUDIT_KUMA_TOKEN", "")
    if not token:
        secrets = Path(os.environ.get("MANITOBA_SECRETS_DIR",
                                      str(Path.home() / "secrets")))
        try:
            data = json.loads((secrets / "kuma-push-tokens.json").read_text(encoding="utf-8"))
            token = data.get(KUMA_PUSH_KEY, "") or ""
        except (OSError, ValueError):
            token = ""
    if not token:
        print("[qflix-audit] WARNING: no Kuma push token under '" + KUMA_PUSH_KEY
              + "' - heartbeat NOT pushed (monitor '" + KUMA_MONITOR + "' will go red, correctly)",
              file=sys.stderr)
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200]})
    try:
        urllib.request.urlopen(KUMA_BASE + "/api/push/" + token + "?" + qs, timeout=5).read()
    except Exception as exc:  # noqa: BLE001 - network shape varies; reported, not hidden
        print("[qflix-audit] WARNING: Kuma push failed (non-fatal): " + str(exc),
              file=sys.stderr)


def _human(report: dict) -> str:
    out = []
    s = report["summary"]
    out.append("qflix-audit  digest=" + report["report_digest"])
    out.append("  commit=" + report["meta"]["commit"] + "  generated_at=" + report["meta"]["generated_at"])
    out.append("")
    out.append("  {:<6} {:<34} {:>8} {:>6} {:>8} {:>7}".format(
        "class", "title", "boundary", "ok", "findings", "waived"))
    for c in report["coverage"]:
        out.append("  {:<6} {:<34} {:>8} {:>6} {:>8} {:>7}  [{}]".format(
            c["class"], c["title"][:34], c["boundary_size"],
            c["tally"]["ok"], c["tally"]["finding"], c["tally"]["waived"], c["status"]))
    out.append("")
    out.append("  enumerated " + str(s["instances_enumerated"]) + " instances across "
               + str(s["classes_offline"]) + " offline classes; "
               + str(s["classes_residual"]) + " live classes are RESIDUAL "
               "(see docs/audit-residual-risk.md)")
    out.append("  findings: " + str(s["findings_enforced"]) + " enforced, "
               + str(s["findings_advisory"]) + " advisory; " + str(s["waived"]) + " waived")
    absent = [p for p, here in sorted(report["meta"]["s2_subjects"].items()) if not here]
    if absent:
        out.append("")
        out.append("  S2 SUBJECTS ABSENT ON THIS HOST (their cross-checks DID NOT RUN — residual R4):")
        for p in absent:
            out.append("    " + p)
    if report["findings"]:
        out.append("")
        for f in report["findings"]:
            if f["severity"] == "enforced":
                out.append("  ENFORCED " + f["class"] + " " + f["instance_id"] + " — " + f["detail"])
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Deterministic offline audit of the QFlix repo.")
    ap.add_argument("--json", action="store_true", help="emit the machine report")
    ap.add_argument("--digest-only", action="store_true", help="print just report_digest")
    ap.add_argument("--push-kuma", action="store_true", help="push a heartbeat to Kuma")
    ap.add_argument("--root", default=None, help="repo root (default: auto-detect)")
    ap.add_argument("--today", default=None,
                    help="YYYY-MM-DD for residual-cadence checks (never affects the digest)")
    args = ap.parse_args(argv)

    today = _dt.date.fromisoformat(args.today) if args.today else None
    repo = Repo(Path(args.root) if args.root else find_repo_root())

    try:
        report = run(repo, today=today)
    except RegimeError as exc:
        print("[qflix-audit] REGIME INTEGRITY FAILURE (exit 2)", file=sys.stderr)
        print(str(exc), file=sys.stderr)
        if args.push_kuma:
            _push_kuma("down", "REGIME INTEGRITY: " + str(exc).split("\n")[0])
        return EXIT_REGIME

    code = exit_code_for(report)
    _write_audit_log(report["audit_log"], report["report_digest"])

    if args.digest_only:
        print(report["report_digest"])
    elif args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_human(report))

    if args.push_kuma:
        s = report["summary"]
        msg = ("digest=" + report["report_digest"][:16] + " enforced=" + str(s["findings_enforced"])
               + " advisory=" + str(s["findings_advisory"]) + " waived=" + str(s["waived"]))
        _push_kuma("up" if code == EXIT_CLEAN else "down", msg)
    return code


if __name__ == "__main__":
    sys.exit(main())

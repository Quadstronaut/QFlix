"""lib.audit — the Convergent Audit Regime.

A program that ENUMERATES defects instead of a model that searches for them.

The operator-facing property is RUN-TO-RUN DETERMINISM, not zero findings:
two runs at the same commit produce a byte-identical `report_digest`, so
"will you find new stuff?" becomes "did the digest change?" — one hex string,
comparable by eye. A finding can only be new when an input changed or a class
was newly enrolled, and both are attributable.

What this package deliberately does NOT do: promise no new findings ever.
See docs/audit-residual-risk.md — R1..R6 remain, forever, by construction.

Layout:
  repo.py       filesystem + git access (the only I/O), glob matching
  model.py      Verdict / DetectorResult / canonical JSON / digest + hygiene
  ledger.py     loads and validates the four YAML ledgers; raises RegimeError
  engine.py     runs detectors, applies waivers, builds the report
  detectors/    one module per enrolled defect class
"""
from __future__ import annotations

from .model import Verdict, DetectorResult, RegimeError

__all__ = ["Verdict", "DetectorResult", "RegimeError"]

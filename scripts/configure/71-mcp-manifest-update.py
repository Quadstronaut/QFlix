#!/usr/bin/env python3
"""scripts/configure/71-mcp-manifest-update.py

Idempotently adds the `qflix-missing-search` cron-class app to manifest/apps.yaml.

Uses surgical TEXT-LEVEL insertion (NOT yaml.safe_dump round-trip) to preserve
existing comments, inline-array style, and whitespace. Inserts the new entry
immediately before the `canaries:` top-level section.
"""
from __future__ import annotations
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "manifest" / "apps.yaml"

NEW_ENTRY = """  qflix-missing-search:
    class: cron
    unit: qflix-missing-search.service
    kuma_monitor: "Qflix Missing Search"
    health:
      kind: systemd_oneshot
      unit: qflix-missing-search.service

"""

INSERTION_KEY = "  qflix-missing-search:"
ANCHOR = "canaries:"


def main() -> int:
    text = MANIFEST.read_text(encoding="utf-8")
    if INSERTION_KEY in text:
        print("OK: qflix-missing-search already in manifest")
        return 0
    # Find the `canaries:` top-level anchor and insert immediately above it.
    lines = text.splitlines(keepends=True)
    anchor_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.startswith(ANCHOR)),
        None,
    )
    if anchor_idx is None:
        print("ERROR: could not find 'canaries:' anchor in manifest", file=sys.stderr)
        return 1
    # Walk back over any blank-line padding so we land right after the last
    # app entry. We'll add the new entry, then re-insert one blank before
    # canaries:.
    back = anchor_idx
    while back > 0 and lines[back - 1].strip() == "":
        back -= 1
    new_lines = lines[:back] + [NEW_ENTRY] + lines[back:]
    MANIFEST.write_text("".join(new_lines), encoding="utf-8")
    print("ADDED: qflix-missing-search -> manifest/apps.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())

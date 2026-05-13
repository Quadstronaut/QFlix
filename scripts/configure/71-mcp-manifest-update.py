#!/usr/bin/env python3
"""scripts/configure/71-mcp-manifest-update.py

Idempotently adds the `qflix-missing-search` cron-class app to manifest/apps.yaml
so the maintenance pipeline knows to monitor it.
"""
from __future__ import annotations
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
MANIFEST = REPO / "manifest" / "apps.yaml"

NEW_ENTRY = {
    "class": "cron",
    "unit": "qflix-missing-search.service",
    "kuma_monitor": "Qflix Missing Search",
    "health": {"kind": "systemd_oneshot", "unit": "qflix-missing-search.service"},
}


def main() -> int:
    text = MANIFEST.read_text()
    data = yaml.safe_load(text)
    if "qflix-missing-search" in (data.get("apps") or {}):
        print("OK: qflix-missing-search already in manifest")
        return 0
    data.setdefault("apps", {})["qflix-missing-search"] = NEW_ENTRY
    MANIFEST.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    )
    print("ADDED: qflix-missing-search → manifest/apps.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())

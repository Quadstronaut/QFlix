"""QFLX-87: the 90% emergency reclaim must not be stricter than the nightly reaper.

quota.sh ran `qflix-reaper.py --execute --json` with the reaper's default
--max-pct 30, while the box's armed nightly unit runs --max-pct 100. The 30%
per-library tripwire aborts the WHOLE run before any delete, so on a small
library the emergency path could never reclaim a byte and re-paged every 15 min.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _reaper_argv(text: str) -> str:
    m = re.search(r"qflix-reaper\.py([^)\n]*)", text)
    assert m, "quota.sh no longer invokes qflix-reaper.py"
    return m.group(1)


def test_quota_reclaim_executes_with_tripwire_disabled():
    argv = _reaper_argv((REPO / "scripts" / "canaries" / "quota.sh").read_text(encoding="utf-8"))
    assert "--execute" in argv
    assert re.search(r"--max-pct\s+100\b", argv), argv


def test_quota_reclaim_keeps_the_max_items_rate_cap():
    argv = _reaper_argv((REPO / "scripts" / "canaries" / "quota.sh").read_text(encoding="utf-8"))
    assert "--max-items" not in argv, "rate cap must stay at the reaper default"
    assert "--force" not in argv

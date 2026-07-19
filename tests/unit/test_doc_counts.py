"""Doc-vs-manifest drift guard.

README.md and inventory.md quote headline numbers — app count, canary count,
Kuma monitor totals — that are *supposed* to track manifest/apps.yaml. They
have silently drifted before (canaries 9 vs 12, monitors 43 vs 46). These
tests recompute the numbers from the manifest (the single source of truth)
and assert each doc still agrees. When a number diverges, the failure names
both sides so the fix is obvious.

If a doc is reworded so a regex stops matching, the test fails loudly on the
missing anchor — that's intentional: restructure the doc and the guard
together.
"""
import os
import re

from lib.manifest import load

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
APPS_YAML = os.path.join(REPO_ROOT, "manifest", "apps.yaml")
README = os.path.join(REPO_ROOT, "README.md")
INVENTORY = os.path.join(REPO_ROOT, "inventory.md")

# Real Kuma monitors that live OUTSIDE the manifest's app/canary sets but are
# manitoba-owned and counted in every "manitoba monitors" total in the docs.
# `manitoba-maint kuma audit` counts them (matched), so the docs must too to
# reflect the LIVE monitor set:
#   - "Manitoba Pusher"           — the pusher's own self-heartbeat (step 0b)
#   - "QFlix Fleet"               — the fleet-aggregate storm monitor (step 0c)
#   - "QFlix Reaper"              — the reaper's self-pushed daily monitor
#   - "QFlix Audio Disposition"   — the audio-disposition janitor's self-pushed
#                                   daily monitor (#102, added 2026-07-19)
NON_MANIFEST_MONITORS = 4


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _expected():
    m = load(APPS_YAML)
    apps = list(m.apps())
    canaries = list(m.canaries())
    manitoba = len(m.all_kuma_monitor_names()) + NON_MANIFEST_MONITORS
    total = manitoba + len(m.external_monitors())
    return {
        "apps": len(apps),
        "canaries": len(canaries),
        "manitoba_monitors": manitoba,
        "total_monitors": total,
    }


def _grab(text, pattern, label):
    """First capture group of `pattern` as int, or fail naming the anchor."""
    match = re.search(pattern, text)
    assert match, f"could not find {label} in doc (anchor moved? update this guard)"
    return int(match.group(1))


def test_readme_counts_match_manifest():
    exp = _expected()
    r = _read(README)

    assert _grab(r, r"badge/manifest-(\d+)_apps", "manifest apps badge") == exp["apps"]

    kuma_badge = re.search(r"badge/Kuma-(\d+)%2F(\d+)_up", r)
    assert kuma_badge, "could not find Kuma badge in README"
    assert int(kuma_badge.group(1)) == exp["manitoba_monitors"]
    assert int(kuma_badge.group(2)) == exp["manitoba_monitors"]

    assert _grab(
        r, r"End-to-end canaries \([^)]*\)\s*\|\s*\*\*(\d+)\*\*", "canary at-a-glance row"
    ) == exp["canaries"]

    assert _grab(
        r, r"Kuma push monitors \(manitoba-owned\)\s*\|\s*\*\*(\d+)\*\*", "monitor at-a-glance row"
    ) == exp["manitoba_monitors"]

    assert _grab(
        r, r"#\s*(\d+)\s+apps\s*\+\s*\d+\s+canaries", "repo-layout apps comment"
    ) == exp["apps"]
    assert _grab(
        r, r"#\s*\d+\s+apps\s*\+\s*(\d+)\s+canaries", "repo-layout canaries comment"
    ) == exp["canaries"]


def test_inventory_counts_match_manifest():
    exp = _expected()
    inv = _read(INVENTORY)

    assert _grab(inv, r"\*\*(\d+) apps in", "inventory app count") == exp["apps"]
    assert _grab(inv, r"\+\s*(\d+) canaries", "inventory canary count") == exp["canaries"]
    assert _grab(inv, r"\*\*(\d+) Kuma monitors\*\* total", "inventory total monitors") == exp["total_monitors"]
    assert _grab(inv, r"\*\*(\d+) manitoba\*\*", "inventory manitoba monitors") == exp["manitoba_monitors"]

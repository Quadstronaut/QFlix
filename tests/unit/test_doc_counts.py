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
FAQ = os.path.join(REPO_ROOT, "scripts", "data", "qflix-faq.html")

# Real Kuma monitors that live OUTSIDE the manifest's app/canary sets but are
# manitoba-owned and counted in every "manitoba monitors" total in the docs.
# `manitoba-maint kuma audit` counts them (matched), so the docs must too to
# reflect the LIVE monitor set:
#   - "Manitoba Pusher"           — the pusher's own self-heartbeat (step 0b)
#   - "QFlix Fleet"               — the fleet-aggregate storm monitor (step 0c)
#   - "QFlix Reaper"              — the reaper's self-pushed daily monitor
#   - "QFlix Audio Disposition"   — the audio-disposition janitor's self-pushed
#                                   daily monitor (#102, added 2026-07-19)
#   - "qflix-anime-janitor"       — the anime-janitor's self-pushed daily monitor
#   - "QFlix Torrent Janitor"     — the torrent-janitor's self-pushed daily
#                                   monitor (added 2026-07-27). The 2026-07-27
#                                   audit registered every self-pusher in
#                                   lib/kuma.py's STANDALONE_SELF_PUSH_MONITORS
#                                   so `kuma audit` stops flagging anime-janitor
#                                   + audio-disposition as orphan drift.
#   - "QFlix Collect (workstation)" — the box-side hourly collector's own
#                                   self-pushed monitor. Moved INTO
#                                   STANDALONE_SELF_PUSH_MONITORS 2026-07-29
#                                   (previously counted via
#                                   manifest.external_monitors() instead —
#                                   same grand total, different bucket; see
#                                   lib/kuma.py and manifest/apps.yaml).
#   - "QFlix Audit Regime"        — the Convergent Audit Regime's own dead-man
#                                   (2026-07-29). qflix-audit.py self-pushes it
#                                   daily from manitoba-maint-audit.timer. It is
#                                   DECLARED here and in
#                                   STANDALONE_SELF_PUSH_MONITORS but not yet
#                                   created live: `kuma audit` reports it as
#                                   manifest_only until bootstrap runs on the
#                                   box, which is a true finding rather than
#                                   drift noise. See docs/audit-regime.md.
NON_MANIFEST_MONITORS = 8


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


def test_faq_canary_count_matches_manifest():
    """The public FAQ (scripts/data/qflix-faq.html) quotes the canary count in
    THREE places; the 14->15 bump (b3c3fd3) missed one (council 2026-07-20,
    Defect 4). This guard closes the gap the earlier README/inventory-only
    guards left open: every "<N> canaries" / "<N> end-to-end canaries" phrase
    in the FAQ must equal the manifest count, and no stale off-by-one may
    linger."""
    exp = _expected()
    faq = _read(FAQ)
    # Only the TOTAL-count anchors — not historical subset references like
    # "the 4 canaries were wired only to the auto-heal one" (line ~1095).
    anchors = [
        (r"(\d+)\s+end-to-end canaries", "FAQ deck-sub 'N end-to-end canaries'"),
        (r"What do the (\d+) canaries actually test", "FAQ canary-table heading"),
        (r"all (\d+) canaries", "FAQ app-liveness 'all N canaries'"),
    ]
    for pat, label in anchors:
        assert _grab(faq, pat, label) == exp["canaries"]


# --- identity guards (2026-07-28) --------------------------------------------
# Counting alone was never enough. The 17->18 bump was caught by the count
# guards above, but an audit that same day found drift the counts could not
# see: README quoted "15 canaries" in two prose spots, the FAQ's per-canary
# TABLE listed only 15 of 17 rows, scripts/canaries/README.md was missing 4
# entries outright, and two FAQ cadences (quota, prowlarr-indexer-health) still
# said "hourly" for canaries that had long since moved to every-15min. A doc can
# hold the right total and still describe the wrong system, so these guards
# assert the NAMES and CADENCES, not just the count.

CANARY_README = os.path.join(REPO_ROOT, "scripts", "canaries", "README.md")


def _manifest_canaries():
    return list(load(APPS_YAML).canaries())


def _faq_canary_table():
    """{name: cadence} parsed from the FAQ's per-canary table."""
    faq = _read(FAQ)
    i = faq.find("canaries actually test")
    assert i != -1, "FAQ canary-table heading moved (update this guard)"
    seg = faq[i:i + 12000]
    body = seg[seg.find("<tbody>"):seg.find("</tbody>")]
    return {
        name: cadence.strip()
        for name, cadence in re.findall(
            r"<code>canary-([a-z0-9-]+)</code></td><td>([^<]*)</td>", body
        )
    }


def _norm_cadence(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def test_readme_prose_canary_counts_match_manifest():
    """README quotes the canary count in prose too, not just the tables the
    original guards checked. Both said 15 while the manifest said 17."""
    exp = _expected()
    r = _read(README)
    for pat, label in [
        (r"manitoba-maint, (\d+) canaries", "README surrounding-cast prose"),
        (r"canaries/\s*#\s*(\d+) end-to-end pipeline checks", "README repo-layout comment"),
    ]:
        assert _grab(r, pat, label) == exp["canaries"]


def test_faq_canary_table_lists_every_canary():
    """The FAQ's per-canary table must name exactly the manifest's canaries.
    It had drifted to 15 rows against 17 manifest entries — the count anchors
    elsewhere on the page were correct, so nothing caught it."""
    table = set(_faq_canary_table())
    manifest_names = {c.name for c in _manifest_canaries()}
    assert table == manifest_names, (
        f"FAQ canary table drift — missing: {sorted(manifest_names - table)}, "
        f"unknown: {sorted(table - manifest_names)}"
    )


def test_faq_canary_cadences_match_manifest():
    """Cadence prose must match the manifest schedule. `quota` and
    `prowlarr-indexer-health` both still advertised 'hourly' long after moving
    to every-15min."""
    table = _faq_canary_table()
    mismatches = []
    for c in _manifest_canaries():
        shown = table.get(c.name)
        if shown is None:
            continue  # covered by the table-membership guard above
        # weekly-mon-send is rendered as the three concrete send-window times,
        # which is MORE precise than the manifest token and matches the timers.
        if c.schedule == "weekly-mon-send":
            assert "mon" in shown.lower(), f"{c.name}: expected a Monday cadence, got {shown!r}"
            continue
        if _norm_cadence(shown) != _norm_cadence(c.schedule):
            mismatches.append(f"{c.name}: manifest={c.schedule!r} FAQ={shown!r}")
    assert not mismatches, "FAQ cadence drift — " + "; ".join(mismatches)


def test_unit_counts_agree_across_every_doc_that_quotes_them():
    """The live systemd unit counts are a BOX measurement, not something the
    manifest can derive - so the only thing a test can enforce is that the three
    places quoting them agree with each other. That is enough: they have moved in
    lockstep through every commit that touched them (36 services/30 timers ->
    55/43 -> 56/44), and the one time they diverged it was a bug.

    On 2026-07-29 the at-a-glance row was bumped to 45 while the repo-layout
    comment nine lines of prose later and the public FAQ both still said 43 - all
    three stamped with the same date. An operator reading top to bottom got three
    different answers and no way to tell which was real. Nothing caught it,
    because no guard existed for numbers the manifest cannot compute.

    Re-measure with exactly:
        ls ~/.config/systemd/user/*.service | wc -l
        ls ~/.config/systemd/user/*.timer   | wc -l
    """
    r = _read(README)
    faq = _read(FAQ)

    at_a_glance = _grab(
        r, r"Cron \+ systemd timers \|\s*\*\*(\d+)\*\*", "README at-a-glance timers row")
    layout = re.search(r"#\s*(\d+) services \+ (\d+) timers", r)
    assert layout, "README repo-layout systemd comment moved (update this guard)"
    layout_services, layout_timers = int(layout.group(1)), int(layout.group(2))
    faq_m = re.search(r"(\d+) services \+ (\d+) timers", faq)
    assert faq_m, "FAQ stack-paragraph unit counts moved (update this guard)"
    faq_services, faq_timers = int(faq_m.group(1)), int(faq_m.group(2))

    assert at_a_glance == layout_timers == faq_timers, (
        "timer count drift - README at-a-glance=%d, README repo-layout=%d, "
        "FAQ=%d. These are the SAME measurement; make all three agree."
        % (at_a_glance, layout_timers, faq_timers))
    assert layout_services == faq_services, (
        "service count drift - README repo-layout=%d, FAQ=%d"
        % (layout_services, faq_services))
    # Sanity floor: the box has more units than the repo has unit FILES, because
    # the panel templates some. A figure below the repo count means someone
    # counted the wrong population.
    repo_units = os.path.join(REPO_ROOT, "scripts", "maint", "systemd")
    repo_timers = len([f for f in os.listdir(repo_units) if f.endswith(".timer")])
    assert layout_timers >= repo_timers, (
        "documented live timer count (%d) is below the number of .timer files in "
        "scripts/maint/systemd/ (%d) - wrong population counted"
        % (layout_timers, repo_timers))


def test_canary_readme_documents_every_canary():
    """scripts/canaries/README.md must carry a bullet for every canary script.
    It was missing sab-stall, thread-ceiling, tdarr-scanner and
    tdarr-healthcheck — 4 undocumented canaries."""
    doc = _read(CANARY_README)
    documented = set(re.findall(r"`([a-z0-9-]+)\.sh`", doc))
    missing = []
    for c in _manifest_canaries():
        stem = os.path.basename(c.script)[:-3]  # strip .sh
        if stem not in documented:
            missing.append(stem)
    assert not missing, f"scripts/canaries/README.md missing entries for: {sorted(missing)}"

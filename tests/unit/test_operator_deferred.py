"""The deferral registry must not silently accumulate or go stale.

WHY (2026-07-30): the operator found a "Phase 5" deferral mentioned in a commit
and said, in effect, that finding one was itself the problem -- QFlix is live and
is several people's primary source of entertainment, so a shipped product with
parts deferred to "a later session" harms the clients.

Auditing docs/operator-deferred.md then showed something worse than a backlog:
**two of its open items were already DONE.**

  Phase 16 uninstalls   claimed "ready to execute"; measured on the box, all 7
                        apps had no dir, no units, no port secret, no nginx
                        fragment. Uninstalled long ago, never marked.
  quality-fallback      claimed its Kuma monitor still needed a bootstrap run.
                        It existed, UP, with both notification channels.

So the registry was itself the silent failure: it made a finished product read
as unfinished, and nobody could tell which entries were real without going and
measuring. A backlog nobody trusts is worse than no backlog, because the real
items hide among the stale ones.

These tests do not judge whether deferring something is acceptable -- that is the
operator's call. They enforce that every open item is ATTRIBUTABLE and DATED, so
a stale one cannot masquerade as work and a real one cannot fade.
"""
import datetime
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "operator-deferred.md"
TODO = REPO / "todo-after-claude.md"


def _text():
    return DOC.read_text(encoding="utf-8")


def _open_section():
    """Everything under the '## Open' heading up to the next '## '."""
    t = _text()
    m = re.search(r"^## Open\b.*?$", t, re.M)
    assert m, "operator-deferred.md no longer has an '## Open' section"
    rest = t[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def _open_items():
    """### headings inside the Open section."""
    return re.findall(r"^### (.+)$", _open_section(), re.M)


def test_registry_exists_and_states_its_own_stakes():
    """A backlog for a LIVE product has to say so, or it reads as a wishlist."""
    t = _text()
    assert "live" in t.lower(), "the file no longer says the product is live"
    assert "not finished" in t.lower() or "unfinished" in t.lower(), (
        "the file no longer states that an entry here means shipped-but-unfinished"
    )


def test_every_open_item_has_an_owner_and_a_dated_reason():
    """Unattributed items are how a backlog rots.

    Each open item must name who decides and when it was last adjudicated, so a
    reader can tell a live decision from an abandoned one WITHOUT going and
    measuring the box -- which is exactly what nobody did for months.
    """
    sec = _open_section()
    for item in _open_items():
        body = sec.split("### " + item, 1)[1]
        nxt = re.search(r"^### ", body, re.M)
        body = body[: nxt.start()] if nxt else body
        assert re.search(r"\*\*Owner:\*\*", body), f"open item lacks an owner: {item}"
        assert re.search(r"20\d\d-\d\d-\d\d", body), (
            f"open item lacks a dated adjudication: {item}"
        )


def test_the_registry_has_been_reconciled_against_the_box():
    """A registry nobody re-checks is the failure mode this file caused.

    Two entries sat 'open' for months while already done. The header must carry
    a reconciliation date so staleness is visible at a glance.
    """
    m = re.search(r"[Ll]ast reconciled.*?(20\d\d-\d\d-\d\d)", _text())
    assert m, "operator-deferred.md has no 'Last reconciled' date"
    when = datetime.date.fromisoformat(m.group(1))
    assert when >= datetime.date(2026, 7, 30), (
        "reconciliation date went backwards: " + m.group(1)
    )


def test_open_items_do_not_grow_unnoticed():
    """A ratchet, not a limit.

    One open item today (the redundant Notifiarr path, a standing choice). This
    fails if the count climbs, forcing a deliberate decision to raise the bound
    rather than letting deferrals pile up one commit at a time.
    """
    items = _open_items()
    assert len(items) <= 1, (
        "deferred items grew to %d: %s -- finish them or raise this bound "
        "deliberately, in a commit that says why" % (len(items), items)
    )


def test_no_item_defers_to_a_later_session():
    """'Phase 5', 'next session', 'later' are not states.

    The operator's standing instruction: the live product is always finished.
    Work is done, or closed with a decision, or blocked on the operator with the
    reason stated -- never parked for a future session.
    """
    banned = [r"later session", r"next session", r"[Pp]hase\s*5\b",
              r"belongs to the Phase", r"future session"]
    hay = _text()
    for pat in banned:
        assert not re.search(pat, hay), (
            "operator-deferred.md defers to a later session (%r) -- close it or "
            "state the operator-side blocker" % pat
        )


def test_the_side_todo_file_does_not_reintroduce_a_backlog():
    """todo-after-claude.md must not become a second, unwatched registry."""
    if not TODO.exists():
        return
    t = TODO.read_text(encoding="utf-8")
    bullets = [b for b in re.findall(r"^\s*[*-]\s+(.+)$", t, re.M)
               if not b.lower().startswith("nothing else")]
    for b in bullets:
        assert re.search(r"(?i)(closed|resolved|done|see docs/operator-deferred)", b), (
            "todo-after-claude.md carries an unresolved item that is not tracked "
            "in the deferral registry: " + b[:90]
        )

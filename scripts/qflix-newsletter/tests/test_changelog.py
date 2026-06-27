"""Unit tests for the Behind-the-scenes changelog module.

All offline: GitHub fetches are monkeypatched. Covers conventional-commit
parsing, the friendly-trailer override, grouping/caps, the digest-branch
override + freshness guard, and the override-then-fallback orchestration.
"""
from __future__ import annotations

import datetime as _dt

import pytest

from qflix_newsletter import changelog as C


def _now() -> _dt.datetime:
    return _dt.datetime(2026, 6, 27, 15, 0, tzinfo=_dt.timezone.utc)


class _Resp:
    """Minimal stand-in for a requests.Response."""

    def __init__(self, status: int = 200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.text = ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _commit(type_: str, desc: str) -> C.Commit:
    return C.Commit(sha="x", type=type_, scope=None, summary=desc, friendly=None)


# --- parse_commit ---------------------------------------------------------


def test_parse_conventional_strips_scope():
    c = C.parse_commit("abc", "fix(vlogs): cap GOMAXPROCS=4 to stop crash-loop")
    assert c.type == "fix"
    assert c.scope == "vlogs"
    assert c.summary == "cap GOMAXPROCS=4 to stop crash-loop"
    assert c.display == "cap GOMAXPROCS=4 to stop crash-loop"


def test_parse_feat_without_scope():
    c = C.parse_commit("abc", "feat: add Usenet downloads")
    assert c.type == "feat"
    assert c.scope is None
    assert c.summary == "add Usenet downloads"


def test_trailer_overrides_display():
    msg = "fix(vlogs): cap GOMAXPROCS=4\n\nbody\nNewsletter: Improved streaming stability"
    c = C.parse_commit("abc", msg)
    assert c.friendly == "Improved streaming stability"
    assert c.display == "Improved streaming stability"


def test_bang_breaking_change_still_parses_type():
    c = C.parse_commit("abc", "refactor(newsletter)!: retire Gemini")
    assert c.type == "refactor"
    assert c.summary == "retire Gemini"


def test_non_conventional_falls_back_to_subject():
    c = C.parse_commit("abc", "random commit message")
    assert c.type == ""
    assert c.summary == "random commit message"


# --- build_behind_scenes --------------------------------------------------


def test_groups_feat_fix_and_counts_other():
    commits = [
        _commit("feat", "a"),
        _commit("fix", "b"),
        _commit("perf", "c"),
        _commit("docs", "d"),
        _commit("chore", "e"),
        _commit("", "f"),
    ]
    bs = C.build_behind_scenes(commits)
    assert bs.feature_count == 1
    assert bs.fix_count == 2  # fix + perf
    assert bs.other_count == 3  # docs, chore, non-conventional
    assert bs.has_items


def test_bullets_capped_but_counts_full():
    commits = [_commit("fix", f"f{i}") for i in range(10)]
    bs = C.build_behind_scenes(commits, max_bullets=3)
    assert len(bs.fixes) == 3
    assert bs.fix_count == 10
    assert bs.fix_overflow == 7


def test_empty_has_no_items():
    assert not C.build_behind_scenes([]).has_items


# --- fetch_commits (mocked) ----------------------------------------------


def test_fetch_commits_parses_and_skips_merges(monkeypatch):
    payload = [
        {"sha": "1", "commit": {"message": "feat: new thing", "author": {"date": "2026-06-25T10:00:00Z"}}},
        {"sha": "2", "commit": {"message": "Merge pull request #3", "author": {"date": "2026-06-25T10:00:00Z"}}},
        {"sha": "3", "commit": {"message": "fix(x): a fix", "author": {"date": "2026-06-26T10:00:00Z"}}},
    ]
    monkeypatch.setattr(C.requests, "get", lambda *a, **k: _Resp(200, payload))
    commits = C.fetch_commits("o/r", now=_now())
    assert [c.type for c in commits] == ["feat", "fix"]  # merge dropped


def test_fetch_commits_network_error_propagates(monkeypatch):
    def boom(*a, **k):
        raise C.requests.RequestException("down")

    monkeypatch.setattr(C.requests, "get", boom)
    with pytest.raises(Exception):
        C.fetch_commits("o/r", now=_now())


# --- fetch_override (mocked) ---------------------------------------------


def test_override_used_when_fresh(monkeypatch):
    payload = {"week_of": "2026-06-27", "html": "<p>We improved streaming.</p>"}
    monkeypatch.setattr(C.requests, "get", lambda *a, **k: _Resp(200, payload))
    bs = C.fetch_override("o/r", now=_now())
    assert bs is not None and bs.has_blurb
    assert "improved streaming" in bs.blurb_html.lower()


def test_override_stale_is_rejected(monkeypatch):
    payload = {"week_of": "2026-06-01", "html": "<p>old</p>"}  # weeks old
    monkeypatch.setattr(C.requests, "get", lambda *a, **k: _Resp(200, payload))
    assert C.fetch_override("o/r", now=_now()) is None


def test_override_404_is_none(monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda *a, **k: _Resp(404, {}))
    assert C.fetch_override("o/r", now=_now()) is None


def test_override_empty_html_is_none(monkeypatch):
    monkeypatch.setattr(C.requests, "get", lambda *a, **k: _Resp(200, {"week_of": "2026-06-27", "html": ""}))
    assert C.fetch_override("o/r", now=_now()) is None


# --- fetch_behind_scenes orchestration -----------------------------------


def test_fallback_to_deterministic_when_no_override(monkeypatch):
    def fake_get(url, *a, **k):
        if "raw.githubusercontent" in url:
            return _Resp(404, {})
        return _Resp(200, [{"sha": "1", "commit": {"message": "feat: x", "author": {"date": "2026-06-25T10:00:00Z"}}}])

    monkeypatch.setattr(C.requests, "get", fake_get)
    bs = C.fetch_behind_scenes("o/r", now=_now())
    assert bs is not None and not bs.has_blurb and bs.feature_count == 1


def test_override_preferred_over_commits(monkeypatch):
    def fake_get(url, *a, **k):
        if "raw.githubusercontent" in url:
            return _Resp(200, {"week_of": "2026-06-27", "html": "<p>blurb</p>"})
        return _Resp(200, [{"sha": "1", "commit": {"message": "feat: x", "author": {"date": "2026-06-25T10:00:00Z"}}}])

    monkeypatch.setattr(C.requests, "get", fake_get)
    bs = C.fetch_behind_scenes("o/r", now=_now())
    assert bs is not None and bs.has_blurb


def test_both_empty_returns_none(monkeypatch):
    def fake_get(url, *a, **k):
        if "raw.githubusercontent" in url:
            return _Resp(404, {})
        return _Resp(200, [])

    monkeypatch.setattr(C.requests, "get", fake_get)
    assert C.fetch_behind_scenes("o/r", now=_now()) is None


def test_commit_fetch_failure_hides_section(monkeypatch):
    def fake_get(url, *a, **k):
        if "raw.githubusercontent" in url:
            return _Resp(404, {})
        raise C.requests.RequestException("api down")

    monkeypatch.setattr(C.requests, "get", fake_get)
    assert C.fetch_behind_scenes("o/r", now=_now()) is None

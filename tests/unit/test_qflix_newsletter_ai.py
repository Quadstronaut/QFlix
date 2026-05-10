"""AI picks parsing tests — Gemini JSON shape parsing."""
from __future__ import annotations

from qflix_newsletter import ai


def test_parse_picks_strict_json():
    raw = '[{"if_you_liked":"Dune","try_this":"Foundation","blurb":"Sci-fi epic."}]'
    picks = ai._parse_picks(raw)
    assert len(picks) == 1
    assert picks[0].if_you_liked == "Dune"
    assert picks[0].try_this == "Foundation"
    assert picks[0].blurb == "Sci-fi epic."


def test_parse_picks_strips_markdown_fences():
    raw = """```json
[{"if_you_liked":"Severance","try_this":"Black Mirror","blurb":"Workplace dread."}]
```"""
    picks = ai._parse_picks(raw)
    assert len(picks) == 1
    assert picks[0].try_this == "Black Mirror"


def test_parse_picks_caps_at_three_returns_first_three():
    raw = (
        '[{"if_you_liked":"a","try_this":"a2","blurb":"x"},'
        '{"if_you_liked":"b","try_this":"b2","blurb":"y"},'
        '{"if_you_liked":"c","try_this":"c2","blurb":"z"},'
        '{"if_you_liked":"d","try_this":"d2","blurb":"w"}]'
    )
    picks = ai._parse_picks(raw)
    assert [p.if_you_liked for p in picks] == ["a", "b", "c"]


def test_parse_picks_returns_empty_on_garbage():
    assert ai._parse_picks("hello not json") == []
    assert ai._parse_picks("") == []
    assert ai._parse_picks("{}") == []  # not a list
    assert ai._parse_picks('[{"missing_key":1}]') == []


def test_fetch_ai_picks_returns_empty_when_no_api_key():
    assert ai.fetch_ai_picks(None, ["A"], ["B"]) == []
    assert ai.fetch_ai_picks("", ["A"], ["B"]) == []


def test_build_prompt_includes_titles_and_truncates():
    titles = [f"Title{i}" for i in range(40)]
    recents = [f"Recent{i}" for i in range(20)]
    prompt = ai.build_prompt(titles, recents)
    assert "Title0" in prompt
    assert "Title24" in prompt
    assert "Title25" not in prompt  # cap at 25
    assert "Recent9" in prompt
    assert "Recent10" not in prompt  # cap at 10
    assert "STRICT JSON" in prompt

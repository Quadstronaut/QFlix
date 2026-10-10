"""QFLX-42: re-pin campaign is a dry-run-by-default, draft-only push."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/configure/62-listmonk-repin-campaign.py"
CAMP = ROOT / "scripts/qflix-newsletter/campaigns"


def _load():
    spec = importlib.util.spec_from_file_location("repin_campaign", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _boom(*a, **k):
    raise AssertionError("unexpected network call")


def test_dry_run_default_makes_no_network_call(monkeypatch, capsys):
    m = _load()
    monkeypatch.setattr(m, "lm_req", _boom)
    assert m.main([]) == 0
    assert "dry-run" in capsys.readouterr().out


def test_execute_creates_draft_only(monkeypatch):
    m = _load()
    calls = []

    def fake(path, method="GET", body=None):
        calls.append((path, method, body))
        if method == "GET":
            return 200, {"data": {"results": []}}
        return 200, {"data": {"id": 9}}

    monkeypatch.setattr(m, "lm_req", fake)
    assert m.main(["--execute"]) == 0
    posts = [c for c in calls if c[1] == "POST"]
    assert len(posts) == 1 and posts[0][0] == "/campaigns"
    assert not [c for c in calls if c[1] == "PUT" or "status" in c[0]]
    assert "send_at" not in posts[0][2]


def test_execute_is_idempotent(monkeypatch):
    m = _load()

    def fake(path, method="GET", body=None):
        if method != "GET":
            raise AssertionError("POST on existing campaign")
        return 200, {"data": {"results": [{"name": m.CAMPAIGN_NAME, "id": 1}]}}

    monkeypatch.setattr(m, "lm_req", fake)
    assert m.main(["--execute"]) == 0


def test_templates_content_rules():
    m = _load()
    p = m.build_payload()
    allowed = {"app.plex.tv", "quadstronaut.seedbox.example.com",
               "seerr-quadstronaut.seedbox.example.com"}
    for text in (p["body"], p["altbody"]):
        assert "{{ .Subscriber.FirstName }}" in text
        assert "Seerr" in text and not re.search("jellyseerr", text, re.I)
        assert "does not carry over" in text
        hosts = set(re.findall(r"https?://([^/\s\"'<>]+)", text))
        assert hosts <= allowed, hosts - allowed

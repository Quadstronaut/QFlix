"""patreon-report.py -- DEFECT-A regression (AC-11) and token-rotation safety
(AC-12).

Loaded by path, same convention as test_entitlement_expired_alert.py: it is a
script (hyphenated filename), not an importable package.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAINT = os.path.join(REPO_ROOT, "scripts", "maint")
LIB = os.path.join(MAINT, "lib")


@pytest.fixture(scope="module")
def mod():
    for p in (LIB, MAINT):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(
        "patreon_report_undertest", os.path.join(MAINT, "patreon-report.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["patreon_report_undertest"] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture(scope="module")
def MEM():
    for p in (LIB, MAINT):
        if p not in sys.path:
            sys.path.insert(0, p)
    from lib import members as members_mod
    return members_mod


def _roster(MEM, *, hid="acme", payer_ref="Jane Patreon"):
    hh = MEM.Household(
        id=hid, display="Acme House", exempt=False,
        accounts=["member@example.com"],
        billing=MEM.Billing(holder="member@example.com", amount_usd=5.0,
                            rail="patreon", payer_ref=payer_ref))
    return MEM.Roster(version=1, armed=False, grace_days=3,
                      paused_sections=[], households=[hh])


def _patron(full_name="Jane Patreon", email="jane@example.com",
           status="active_patron"):
    return {
        "id": "pm1",
        "attributes": {
            "patron_status": status,
            "currently_entitled_amount_cents": 500,
            "last_charge_date": "2026-08-01T00:00:00Z",
            "last_charge_status": "Paid",
            "full_name": full_name,
            "email": email,
            "pledge_relationship_start": "2026-01-01T00:00:00Z",
        },
    }


# --- AC-11: DEFECT-A ------------------------------------------------------


def test_reconcile_returns_the_real_household_id_not_none(mod, MEM):
    roster = _roster(MEM)
    rep = mod.reconcile([_patron()], roster)
    assert rep["matched"], "the patron should have matched the roster household"
    assert rep["matched"][0]["household_id"] == "acme"


def test_reconcile_does_not_raise_attributeerror(mod, MEM):
    """THE regression. Household has no `.hid` attribute; the old code raised
    AttributeError on the very first rail=patreon household, uncaught by
    main()'s PatreonError handler."""
    roster = _roster(MEM)
    try:
        mod.reconcile([_patron()], roster)
    except AttributeError as e:
        pytest.fail("reconcile() raised AttributeError: %s" % e)


def test_matched_household_is_not_listed_as_on_rail_without_a_patron(mod, MEM):
    roster = _roster(MEM)
    rep = mod.reconcile([_patron()], roster)
    on_rail_ids = {r["household_id"] for r in rep["roster_on_patreon_without_patron"]}
    assert "acme" not in on_rail_ids, (
        "a MATCHED household must not also appear as 'no patron record' -- "
        "that was the direct consequence of hid always being None")


def test_unmatched_rail_patreon_household_is_reported(mod, MEM):
    """The negative case still works: a real rail=patreon household with NO
    matching patron record must appear exactly once."""
    roster = _roster(MEM, hid="nobody-pledged")
    rep = mod.reconcile([], roster)
    ids = [r["household_id"] for r in rep["roster_on_patreon_without_patron"]]
    assert ids == ["nobody-pledged"]


def test_reconcile_matches_by_email_too(mod, MEM):
    roster = _roster(MEM, payer_ref="jane@example.com")
    rep = mod.reconcile([_patron(full_name="Someone Else",
                                 email="jane@example.com")], roster)
    assert rep["matched"][0]["household_id"] == "acme"


# --- AC-12: token rotation safety -----------------------------------------


def test_api_get_401_without_allow_rotation_never_calls_refresh(mod, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "_get", lambda url, token: (401, "{}"))
    monkeypatch.setattr(mod, "refresh",
                        lambda creds, path: calls.append(1) or creds)
    creds = {"client_id": "c", "client_secret": "s",
             "access_token": "a", "refresh_token": "r"}
    with pytest.raises(mod.AuthError) as ei:
        mod.api_get("https://x/y", creds, None, allow_rotation=False)
    assert calls == [], "refresh() must NEVER be called without explicit opt-in"
    assert "allow-token-rotation" in str(ei.value)
    assert "shared" in str(ei.value).lower() or "SAME" in str(ei.value)


def test_shared_client_hazard_text_names_oauth_and_single_use(mod):
    assert "OAuth" in mod.SHARED_CLIENT_HAZARD
    assert "single-use" in mod.SHARED_CLIENT_HAZARD.lower()


def test_api_get_401_with_allow_rotation_does_call_refresh(mod, monkeypatch, tmp_path):
    creds = {"client_id": "c", "client_secret": "s",
             "access_token": "a-stale", "refresh_token": "r"}
    refreshed = dict(creds, access_token="a-fresh")
    calls = {"get": 0, "refresh": 0}

    def fake_get(url, token):
        calls["get"] += 1
        if token == "a-stale":
            return 401, "{}"
        return 200, json.dumps({"data": [{"id": "camp1"}]})

    def fake_refresh(c, path):
        calls["refresh"] += 1
        return refreshed

    monkeypatch.setattr(mod, "_get", fake_get)
    monkeypatch.setattr(mod, "refresh", fake_refresh)
    data, out_creds = mod.api_get("https://x/y", creds, tmp_path / "c.json",
                                  allow_rotation=True)
    assert calls["refresh"] == 1
    assert out_creds["access_token"] == "a-fresh"
    assert data == {"data": [{"id": "camp1"}]}


def test_verify_creds_lists_names_only(mod):
    creds = {"client_id": "cid-VALUE", "client_secret": "SECRET-VALUE",
             "access_token": "tok-VALUE", "refresh_token": "ref-VALUE"}
    lines = mod.verify_creds(creds)
    text = "\n".join(lines)
    for key in mod.REQUIRED_CRED_KEYS:
        assert key in text
    for secret_value in ("cid-VALUE", "SECRET-VALUE", "tok-VALUE", "ref-VALUE"):
        assert secret_value not in text


def test_verify_creds_flags_missing_keys(mod):
    lines = mod.verify_creds({"client_id": "x"})
    joined = "\n".join(lines)
    assert "client_id" in joined and "present" in joined
    assert "refresh_token" in joined and "MISSING" in joined


def test_main_verify_flag_prints_no_secret_value(mod, tmp_path, capsys):
    creds = {"client_id": "cid-SECRETVALUE", "client_secret": "cs-SECRETVALUE",
             "access_token": "at-SECRETVALUE", "refresh_token": "rt-SECRETVALUE"}
    cpath = tmp_path / "patreon.json"
    cpath.write_text(json.dumps(creds), encoding="utf-8")
    rc = mod.main(["--creds", str(cpath), "--verify"])
    assert rc == mod.EXIT_OK
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for secret_value in ("cid-SECRETVALUE", "cs-SECRETVALUE",
                         "at-SECRETVALUE", "rt-SECRETVALUE"):
        assert secret_value not in combined


def test_main_401_without_rotation_exits_auth_failed_and_names_hazard(
        mod, monkeypatch, tmp_path, capsys):
    creds = {"client_id": "c", "client_secret": "s",
             "access_token": "a", "refresh_token": "r"}
    cpath = tmp_path / "patreon.json"
    cpath.write_text(json.dumps(creds), encoding="utf-8")
    monkeypatch.setattr(mod, "_get", lambda url, token: (401, "{}"))
    monkeypatch.setattr(mod, "refresh", lambda c, p: pytest.fail("must not rotate"))
    rc = mod.main(["--creds", str(cpath)])
    assert rc == mod.EXIT_AUTH_FAILED
    err = capsys.readouterr().err
    assert "allow-token-rotation" in err

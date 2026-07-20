"""Tests for scripts/configure/90b-usenet-all-arrs.py (C7/C8 of the
2026-07-19 SAB stuck-parity spec: usenet everywhere + SAB hardening).

Pure-helper coverage only — payload builders, already-configured predicates,
and the two orchestration functions that are cheap to exercise with a fake
transport. No real network: arr_call/sab_call are monkeypatched wherever a
test needs them, per the module's own transport-vs-logic split.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
spec = importlib.util.spec_from_file_location(
    "usenet_all_arrs",
    ROOT / "scripts" / "configure" / "90b-usenet-all-arrs.py",
)
uaa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(uaa)


# ===========================================================================
# has_enabled_sab_client
# ===========================================================================

def test_has_enabled_sab_client_true_when_present_and_enabled():
    clients = [{"implementation": "Sabnzbd", "enable": True}]
    assert uaa.has_enabled_sab_client(clients) is True


def test_has_enabled_sab_client_false_when_disabled():
    """A Sabnzbd client that exists but is disabled does NOT count — C7
    says 'skip if an ENABLED Sabnzbd client already exists'."""
    clients = [{"implementation": "Sabnzbd", "enable": False}]
    assert uaa.has_enabled_sab_client(clients) is False


def test_has_enabled_sab_client_false_when_absent():
    clients = [{"implementation": "Transmission", "enable": True}]
    assert uaa.has_enabled_sab_client(clients) is False


def test_has_enabled_sab_client_empty_list():
    assert uaa.has_enabled_sab_client([]) is False


def test_has_enabled_sab_client_none():
    assert uaa.has_enabled_sab_client(None) is False


# ===========================================================================
# build_sab_downloadclient_setv — per-kind field-name mapping
# ===========================================================================

def test_downloadclient_setv_tv_fields():
    setv = uaa.build_sab_downloadclient_setv("tv", "sonarr2", "17007", "SABKEY")
    assert setv["tvCategory"] == "sonarr2"
    assert setv["recentTvPriority"] == -100
    assert setv["olderTvPriority"] == -100
    assert "movieCategory" not in setv
    assert setv["host"] == "172.17.0.1"
    assert setv["port"] == 17007          # coerced to int
    assert setv["apiKey"] == "SABKEY"
    assert setv["removeCompletedDownloads"] is True
    assert setv["removeFailedDownloads"] is True


def test_downloadclient_setv_movie_fields():
    setv = uaa.build_sab_downloadclient_setv("movie", "radarr2", "17007", "SABKEY")
    assert setv["movieCategory"] == "radarr2"
    assert setv["recentMoviePriority"] == -100
    assert setv["olderMoviePriority"] == -100
    assert "tvCategory" not in setv


def test_downloadclient_setv_port_is_int_type():
    setv = uaa.build_sab_downloadclient_setv("movie", "radarr", "9999", "k")
    assert isinstance(setv["port"], int)


# ===========================================================================
# _apply_field_values / build_sab_downloadclient_payload
# ===========================================================================

def _schema(*names):
    return {"implementation": "Sabnzbd",
            "fields": [{"name": n, "value": None} for n in names]}


def test_apply_field_values_patches_matching_fields_only():
    schema = _schema("host", "port", "unrelatedField")
    out = uaa._apply_field_values(schema, {"host": "172.17.0.1", "port": 9})
    values = {f["name"]: f["value"] for f in out["fields"]}
    assert values["host"] == "172.17.0.1"
    assert values["port"] == 9
    assert values["unrelatedField"] is None


def test_apply_field_values_does_not_mutate_input():
    schema = _schema("host")
    original = uaa.deepcopy(schema)
    uaa._apply_field_values(schema, {"host": "changed"})
    assert schema == original


def test_build_sab_downloadclient_payload_sets_name_and_enable():
    schema = _schema("host", "port", "apiKey", "tvCategory",
                      "recentTvPriority", "olderTvPriority")
    payload = uaa.build_sab_downloadclient_payload(schema, "tv", "sonarr2", "17007", "K")
    assert payload["name"] == "SABnzbd"
    assert payload["enable"] is True
    values = {f["name"]: f["value"] for f in payload["fields"]}
    assert values["tvCategory"] == "sonarr2"


# ===========================================================================
# has_nzbgeek_indexer / build_nzbgeek_payload
# ===========================================================================

def test_has_nzbgeek_indexer_true():
    assert uaa.has_nzbgeek_indexer([{"name": "NZBgeek"}]) is True


def test_has_nzbgeek_indexer_false():
    assert uaa.has_nzbgeek_indexer([{"name": "Other"}]) is False
    assert uaa.has_nzbgeek_indexer([]) is False


def _newznab_schema():
    return {
        "implementation": "Newznab",
        "id": 999, "infoLink": "http://x", "presets": [{"foo": "bar"}],
        "fields": [{"name": n, "value": None} for n in
                   ("baseUrl", "apiPath", "apiKey", "categories")],
    }


def test_build_nzbgeek_payload_tv_categories():
    payload = uaa.build_nzbgeek_payload(_newznab_schema(), "tv", "https://api.nzbgeek.info", "NZBKEY")
    values = {f["name"]: f["value"] for f in payload["fields"]}
    assert values["categories"] == [5000, 5010, 5020, 5030, 5040, 5045, 5050, 5090]
    assert values["apiKey"] == "NZBKEY"
    assert payload["name"] == "NZBgeek"
    assert payload["priority"] == 25
    assert payload["enableRss"] is True
    assert payload["enableAutomaticSearch"] is True
    assert payload["enableInteractiveSearch"] is True


def test_build_nzbgeek_payload_movie_categories():
    payload = uaa.build_nzbgeek_payload(_newznab_schema(), "movie", "https://api.nzbgeek.info", "K")
    values = {f["name"]: f["value"] for f in payload["fields"]}
    assert values["categories"] == [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060]


def test_build_nzbgeek_payload_strips_add_only_keys():
    payload = uaa.build_nzbgeek_payload(_newznab_schema(), "tv", "url", "k")
    assert "id" not in payload
    assert "infoLink" not in payload
    assert "presets" not in payload


def test_build_nzbgeek_payload_defaults_baseurl_when_empty():
    payload = uaa.build_nzbgeek_payload(_newznab_schema(), "tv", "", "k")
    values = {f["name"]: f["value"] for f in payload["fields"]}
    assert values["baseUrl"] == "https://api.nzbgeek.info"


# ===========================================================================
# delay_profile_patch
# ===========================================================================

def test_delay_profile_patch_none_when_already_enabled():
    profile = {"id": 1, "enableUsenet": True, "preferredProtocol": "torrent"}
    assert uaa.delay_profile_patch(profile) is None


def test_delay_profile_patch_enables_and_sets_delay():
    profile = {"id": 1, "enableUsenet": False, "preferredProtocol": "torrent"}
    patched = uaa.delay_profile_patch(profile)
    assert patched["enableUsenet"] is True
    assert patched["usenetDelay"] == 0
    # preferredProtocol already present -> left alone (C7: not relitigated)
    assert patched["preferredProtocol"] == "torrent"


def test_delay_profile_patch_sets_protocol_only_when_absent():
    profile = {"id": 2, "enableUsenet": False}
    patched = uaa.delay_profile_patch(profile)
    assert patched["preferredProtocol"] == "usenet"


def test_delay_profile_patch_sets_protocol_when_empty_string():
    profile = {"id": 3, "enableUsenet": False, "preferredProtocol": ""}
    patched = uaa.delay_profile_patch(profile)
    assert patched["preferredProtocol"] == "usenet"


def test_delay_profile_patch_does_not_mutate_input():
    profile = {"id": 1, "enableUsenet": False}
    original = uaa.deepcopy(profile)
    uaa.delay_profile_patch(profile)
    assert profile == original


# ===========================================================================
# config_downloadclient_patch (FDH autoRedownloadFailed)
# ===========================================================================

def test_config_downloadclient_patch_none_when_already_true():
    assert uaa.config_downloadclient_patch({"autoRedownloadFailed": True}) is None


def test_config_downloadclient_patch_flips_when_false():
    patched = uaa.config_downloadclient_patch({"id": 1, "autoRedownloadFailed": False})
    assert patched["autoRedownloadFailed"] is True
    assert patched["id"] == 1


def test_config_downloadclient_patch_flips_when_missing():
    patched = uaa.config_downloadclient_patch({"id": 1})
    assert patched["autoRedownloadFailed"] is True


# ===========================================================================
# SAB categories
# ===========================================================================

def test_has_sab_category_true():
    assert uaa.has_sab_category([{"name": "radarr"}, {"name": "sonarr"}], "radarr") is True


def test_has_sab_category_false():
    assert uaa.has_sab_category([{"name": "sonarr"}], "radarr") is False
    assert uaa.has_sab_category([], "radarr") is False


def test_sab_category_params_mirrors_dir_to_keyword():
    params = uaa.sab_category_params("sonarr2")
    assert params["mode"] == "set_config"
    assert params["section"] == "categories"
    assert params["keyword"] == "sonarr2"
    assert params["dir"] == "sonarr2"
    assert params["priority"] == "-100"
    assert params["pp"] == "3"


# ===========================================================================
# history_limit
# ===========================================================================

def test_history_limit_needs_fix_int_ten():
    assert uaa.history_limit_needs_fix(10) is True


def test_history_limit_needs_fix_str_ten():
    assert uaa.history_limit_needs_fix("10") is True


def test_history_limit_needs_fix_int_zero():
    assert uaa.history_limit_needs_fix(0) is False


def test_history_limit_needs_fix_str_zero():
    assert uaa.history_limit_needs_fix("0") is False


def test_history_limit_needs_fix_none():
    """Missing/unparseable value: treat as needing a fix rather than
    silently trusting it."""
    assert uaa.history_limit_needs_fix(None) is True


def test_history_limit_needs_fix_garbage_string():
    assert uaa.history_limit_needs_fix("not-a-number") is True


def test_history_limit_params_shape():
    params = uaa.history_limit_params()
    assert params == {"mode": "set_config", "section": "misc",
                       "keyword": "history_limit", "value": "0"}


# ===========================================================================
# misc_flags_report (report-only, C8)
# ===========================================================================

def test_misc_flags_report_all_ok():
    cfg = {"fail_hopeless_jobs": True, "fast_fail": True, "pause_on_post_processing": True}
    rows = uaa.misc_flags_report(cfg)
    assert len(rows) == 3
    assert all(r["ok"] for r in rows)


def test_misc_flags_report_flags_mismatch():
    cfg = {"fail_hopeless_jobs": False, "fast_fail": True, "pause_on_post_processing": True}
    rows = {r["name"]: r for r in uaa.misc_flags_report(cfg)}
    assert rows["fail_hopeless_jobs"]["ok"] is False
    assert rows["fast_fail"]["ok"] is True


def test_misc_flags_report_missing_keys_are_mismatches():
    rows = {r["name"]: r for r in uaa.misc_flags_report({})}
    assert all(r["ok"] is False for r in rows.values())
    assert rows["fail_hopeless_jobs"]["actual"] is None


# ===========================================================================
# Orchestration functions with a mocked transport (no real network).
# ===========================================================================

def test_ensure_download_client_dry_run_makes_no_post(monkeypatch):
    calls = []

    def fake_arr_call(ctx, method, path, body=None, timeout=30):
        calls.append((method, path))
        if path == "/downloadclient":
            return 200, []
        if path == "/downloadclient/schema":
            return 200, [{"implementation": "Sabnzbd", "fields": []}]
        raise AssertionError("unexpected call: " + path)

    monkeypatch.setattr(uaa, "arr_call", fake_arr_call)
    ctx = {"slug": "radarr", "key": "k", "port": "1", "base": "radarr"}
    line = uaa.ensure_download_client(ctx, "movie", execute=False)
    assert "DRY-RUN" in line
    assert ("POST", "/downloadclient") not in calls


def test_ensure_download_client_execute_posts(monkeypatch):
    posted = {}

    def fake_arr_call(ctx, method, path, body=None, timeout=30):
        if path == "/downloadclient":
            return 200, []
        if path == "/downloadclient/schema":
            return 200, [{"implementation": "Sabnzbd", "fields": []}]
        if method == "POST" and path == "/downloadclient":
            posted["body"] = body
            return 201, {}
        raise AssertionError("unexpected call: " + path)

    monkeypatch.setattr(uaa, "arr_call", fake_arr_call)
    # sabnzbd.port must parse as int (build_sab_downloadclient_setv coerces
    # it); sabnzbd.key can be any opaque string.
    monkeypatch.setattr(uaa, "secret",
                         lambda name: "17007" if name == "sabnzbd.port" else "SECRETVAL")
    ctx = {"slug": "radarr", "key": "k", "port": "1", "base": "radarr"}
    line = uaa.ensure_download_client(ctx, "movie", execute=True)
    assert "added SABnzbd download client" in line


def test_ensure_download_client_skips_when_already_enabled(monkeypatch):
    def fake_arr_call(ctx, method, path, body=None, timeout=30):
        if path == "/downloadclient":
            return 200, [{"implementation": "Sabnzbd", "enable": True}]
        raise AssertionError("should not fetch schema when already configured")

    monkeypatch.setattr(uaa, "arr_call", fake_arr_call)
    ctx = {"slug": "radarr", "key": "k", "port": "1", "base": "radarr"}
    line = uaa.ensure_download_client(ctx, "movie", execute=True)
    assert "already present+enabled" in line


def test_ensure_sab_categories_reports_present_and_missing(monkeypatch):
    def fake_sab_call(params, timeout=40):
        if params.get("section") == "categories":
            return {"config": {"categories": [{"name": "sonarr"}, {"name": "radarr"}]}}
        raise AssertionError("unexpected sab_call: " + str(params))

    monkeypatch.setattr(uaa, "sab_call", fake_sab_call)
    lines = uaa.ensure_sab_categories(execute=False)
    joined = "\n".join(lines)
    assert "category 'radarr' already present" in joined
    assert "DRY-RUN would add category 'sonarr2'" in joined
    assert "DRY-RUN would add category 'radarr2'" in joined


def test_ensure_history_limit_reports_old_value_and_sets(monkeypatch):
    calls = []

    def fake_sab_call(params, timeout=40):
        if params.get("section") == "misc" and params.get("mode") == "get_config":
            return {"config": {"misc": {"history_limit": 10, "fail_hopeless_jobs": True}}}
        calls.append(params)
        return {"status": True}

    monkeypatch.setattr(uaa, "sab_call", fake_sab_call)
    lines, misc_cfg = uaa.ensure_history_limit(execute=True)
    assert any("10 -> 0" in line for line in lines)
    assert misc_cfg["history_limit"] == 10
    assert {"mode": "set_config", "section": "misc",
            "keyword": "history_limit", "value": "0"} in calls


def test_ensure_history_limit_dry_run_makes_no_set_config(monkeypatch):
    def fake_sab_call(params, timeout=40):
        assert params.get("mode") == "get_config", "dry-run must not set_config"
        return {"config": {"misc": {"history_limit": 10}}}

    monkeypatch.setattr(uaa, "sab_call", fake_sab_call)
    lines, _ = uaa.ensure_history_limit(execute=False)
    assert any("DRY-RUN" in line for line in lines)


def test_ensure_history_limit_noop_when_already_zero(monkeypatch):
    def fake_sab_call(params, timeout=40):
        return {"config": {"misc": {"history_limit": 0}}}

    monkeypatch.setattr(uaa, "sab_call", fake_sab_call)
    lines, _ = uaa.ensure_history_limit(execute=True)
    assert any("already 0" in line for line in lines)

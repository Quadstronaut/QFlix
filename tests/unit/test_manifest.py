"""Unit tests for lib/manifest.py — TDD (written before implementation)."""
import os
import pytest
from lib.manifest import load, ManifestError

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "fixtures", "manifests")


def fixture(name):
    return os.path.join(FIXTURES, name)


def test_load_valid():
    m = load(fixture("valid.yaml"))
    apps = list(m.apps())
    assert len(apps) == 3
    for app in apps:
        assert app.class_ in {"ucc", "systemd", "cron", "library"}
    # kuma_monitor may be None (e.g. recyclarr) or a string
    for app in apps:
        assert hasattr(app, "kuma_monitor")


def test_invalid_class():
    with pytest.raises(ManifestError) as exc_info:
        load(fixture("bad-class.yaml"))
    assert "unknown class" in str(exc_info.value).lower()


def test_duplicate_kuma_monitor():
    with pytest.raises(ManifestError) as exc_info:
        load(fixture("duplicate-monitor.yaml"))
    assert "duplicate kuma_monitor" in str(exc_info.value).lower()


def test_missing_required_field():
    with pytest.raises(ManifestError):
        load(fixture("missing-class.yaml"))


def test_max_version_ceiling():
    m = load(fixture("max-version.yaml"))
    app = m.app("tdarr-server")
    assert app.upgrade.max_version == "2.17.01"


def test_resolve_kuma_monitor():
    m = load(fixture("valid.yaml"))
    assert m.resolve_kuma_monitor("Sonarr") == "sonarr"
    assert m.resolve_kuma_monitor("Listmonk") == "listmonk"
    assert m.resolve_kuma_monitor("NonExistent") is None


def test_defaults_inheritance():
    m = load(fixture("valid.yaml"))
    # All apps should inherit top-level defaults
    for app in m.apps():
        assert app.defaults["health_timeout_s"] == 5
        assert app.defaults["recovery_attempts"] == 3
        assert app.defaults["recovery_backoff_s"] == [10, 30, 60]
        assert app.defaults["lifecycle_timeout_s"] == 60
        assert app.defaults["kuma_recheck_delay_s"] == 90

"""Smoke test: loads the real manifest/apps.yaml from repo root."""
import os
from lib.manifest import load

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
APPS_YAML = os.path.join(REPO_ROOT, "manifest", "apps.yaml")

VALID_CLASSES = {"ucc", "systemd", "cron", "library"}


def test_repo_apps_yaml_loads():
    m = load(APPS_YAML)
    apps = list(m.apps())

    # At least 25 apps
    assert len(apps) >= 25, f"Expected >= 25 apps, got {len(apps)}"

    # No duplicate kuma_monitor values (excluding nulls)
    monitors = [a.kuma_monitor for a in apps if a.kuma_monitor is not None]
    assert len(monitors) == len(set(monitors)), "Duplicate kuma_monitor values found"

    # Every app has a valid class
    for app in apps:
        assert app.class_ in VALID_CLASSES, (
            f"App '{app.name}' has invalid class '{app.class_}'"
        )

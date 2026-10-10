"""lib/kuma._kuma_db_path: env > secret > Ultra default (QFLX-41, box-2 native Kuma)."""
from __future__ import annotations

from pathlib import Path

from lib import kuma


def test_default_is_ultra_path(monkeypatch):
    monkeypatch.delenv("QFLIX_KUMA_DB", raising=False)
    assert kuma._kuma_db_path() == Path.home() / ".apps" / "uptimekuma" / "kuma.db"


def test_secret_overrides_default(monkeypatch, tmp_path):
    monkeypatch.delenv("QFLIX_KUMA_DB", raising=False)
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    (tmp_path / "uptimekuma.db-path").write_text("/srv/kuma/data/kuma.db\n")
    assert kuma._kuma_db_path() == Path("/srv/kuma/data/kuma.db")


def test_env_beats_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("MANITOBA_SECRETS_DIR", str(tmp_path))
    (tmp_path / "uptimekuma.db-path").write_text("/srv/kuma/data/kuma.db")
    monkeypatch.setenv("QFLIX_KUMA_DB", "/tmp/x.db")
    assert kuma._kuma_db_path() == Path("/tmp/x.db")

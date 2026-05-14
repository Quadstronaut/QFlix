"""Tests for scripts/mcp/missing.py."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "mcp"))

import missing  # noqa: E402


def _resp(body, status=200):
    m = MagicMock()
    m.status = status
    m.read.return_value = (body if isinstance(body, str) else json.dumps(body)).encode()
    m.__enter__.return_value = m
    return m


@patch("urllib.request.urlopen")
def test_run_dispatches_all_arrs(mock_open, tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"; secrets.mkdir()
    for slug in ("sonarr", "sonarr2", "radarr", "radarr2"):
        (secrets / f"{slug}.key").write_text("KEY")
        (secrets / f"{slug}.port").write_text("17000")
        (secrets / f"{slug}.urlbase").write_text(slug)
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    mock_open.return_value = _resp({"id": 99}, status=201)
    res = missing.run(slug=None)
    assert len(res["per_arr"]) == 4
    assert all(r["status"] == "queued" for r in res["per_arr"].values())


@patch("urllib.request.urlopen")
def test_run_single_slug(mock_open, tmp_path, monkeypatch):
    secrets = tmp_path / "secrets"; secrets.mkdir()
    (secrets / "sonarr.key").write_text("KEY")
    (secrets / "sonarr.port").write_text("17000")
    (secrets / "sonarr.urlbase").write_text("sonarr")
    monkeypatch.setenv("MANITOBA_SECRETS", str(secrets))
    mock_open.return_value = _resp({"id": 99}, status=201)
    res = missing.run(slug="sonarr")
    assert list(res["per_arr"].keys()) == ["sonarr"]

"""Unit tests for canary push (lib/cli.py canary subcommand + lib/manifest.py canaries)."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "maint"))


# ---------------------------------------------------------------------------
# Manifest canary parsing tests
# ---------------------------------------------------------------------------

class TestManifestCanaries:
    def _load(self, yaml_text: str):
        import tempfile
        from lib.manifest import load
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_text)
            name = f.name
        try:
            return load(name)
        finally:
            os.unlink(name)

    _BASE_YAML = """
defaults:
  health_timeout_s: 5
  recovery_attempts: 3
  recovery_backoff_s: [10, 30, 60]
  lifecycle_timeout_s: 60
  kuma_recheck_delay_s: 90

apps:
  sonarr:
    class: ucc
    kuma_monitor: "Sonarr"
    health:
      kind: http_api
      port_secret: sonarr.port
"""

    def test_manifest_loads_canaries(self):
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Canary Movie"
    script: "scripts/canaries/movie.sh"
    schedule: "hourly"
  anime:
    kuma_monitor: "Canary Anime"
    script: "scripts/canaries/anime.sh"
    schedule: "hourly"
"""
        m = self._load(yaml)
        canary_names = [c.name for c in m.canaries()]
        assert "movie" in canary_names
        assert "anime" in canary_names

    def test_manifest_canary_fields_correct(self):
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Canary Movie"
    script: "scripts/canaries/movie.sh"
    schedule: "hourly"
"""
        m = self._load(yaml)
        c = m.canary("movie")
        assert c.kuma_monitor == "Canary Movie"
        assert c.script == "scripts/canaries/movie.sh"
        assert c.schedule == "hourly"

    def test_manifest_canary_lookup_unknown_raises(self):
        m = self._load(self._BASE_YAML)
        with pytest.raises(KeyError):
            m.canary("nonexistent")

    def test_manifest_rejects_duplicate_canary_kuma_monitor(self):
        from lib.manifest import ManifestError
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Canary Dupe"
    script: "scripts/canaries/movie.sh"
    schedule: "hourly"
  anime:
    kuma_monitor: "Canary Dupe"
    script: "scripts/canaries/anime.sh"
    schedule: "hourly"
"""
        with pytest.raises(ManifestError, match="duplicate kuma_monitor"):
            self._load(yaml)

    def test_manifest_rejects_canary_kuma_monitor_collision_with_app(self):
        from lib.manifest import ManifestError
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Sonarr"
    script: "scripts/canaries/movie.sh"
    schedule: "hourly"
"""
        with pytest.raises(ManifestError, match="conflicts with app"):
            self._load(yaml)

    def test_manifest_rejects_invalid_schedule(self):
        from lib.manifest import ManifestError
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Canary Movie"
    script: "scripts/canaries/movie.sh"
    schedule: "monthly"
"""
        with pytest.raises(ManifestError, match="unknown schedule"):
            self._load(yaml)

    def test_manifest_rejects_missing_required_canary_field(self):
        from lib.manifest import ManifestError
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Canary Movie"
    schedule: "hourly"
"""
        with pytest.raises(ManifestError, match="missing required field 'script'"):
            self._load(yaml)

    def test_manifest_no_canaries_section_ok(self):
        m = self._load(self._BASE_YAML)
        assert list(m.canaries()) == []

    def test_manifest_all_kuma_monitor_names_includes_canaries(self):
        yaml = self._BASE_YAML + """
canaries:
  movie:
    kuma_monitor: "Canary Movie"
    script: "scripts/canaries/movie.sh"
    schedule: "hourly"
"""
        m = self._load(yaml)
        names = m.all_kuma_monitor_names()
        assert "Sonarr" in names
        assert "Canary Movie" in names


# ---------------------------------------------------------------------------
# CLI canary push tests
# ---------------------------------------------------------------------------

def _make_manifest_with_canary(name="movie", kuma_monitor="Canary Movie",
                                script="scripts/canaries/movie.sh",
                                schedule="hourly"):
    from lib.manifest import Canary, App, HealthConfig, Manifest
    canary = Canary(name=name, kuma_monitor=kuma_monitor,
                    script=script, schedule=schedule)
    app = App(name="sonarr", class_="ucc", kuma_monitor="Sonarr",
              health=HealthConfig(kind="http_api", raw={}), defaults={})
    return Manifest({"sonarr": app}, {name: canary})


class TestCanaryPushSuccess:
    """canary push success: subprocess exits 0 → status=up pushed to Kuma."""

    def test_canary_push_success_calls_status_up(self, tmp_path, monkeypatch):
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-canary-movie"}')

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")

        import subprocess
        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "PASS: movie canary\n"
        fake_result.stderr = ""

        mock_get = MagicMock()
        resp = MagicMock()
        resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", return_value=fake_result), \
             patch("lib.cli.requests.get", mock_get):
            rc = cli.main(["canary", "push", "movie"], manifest_path=None,
                          _manifest=manifest)

        assert rc == 0
        assert mock_get.called
        params = mock_get.call_args[1]["params"]
        assert params["status"] == "up"
        assert "PASS" in params["msg"]

    def test_canary_push_success_sends_to_correct_token_url(self, tmp_path, monkeypatch):
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-abc123"}')

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "PASS: movie canary\n"
        fake_result.stderr = ""

        mock_get = MagicMock()
        resp = MagicMock(); resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", return_value=fake_result), \
             patch("lib.cli.requests.get", mock_get):
            cli.main(["canary", "push", "movie"], manifest_path=None,
                     _manifest=manifest)

        url = mock_get.call_args[0][0]
        assert url == "http://127.0.0.1:42005/api/push/tok-abc123"


class TestCanaryPushFailure:
    """canary push failure: subprocess exits non-zero → status=down + msg from stderr."""

    def test_canary_push_failure_calls_status_down(self, tmp_path, monkeypatch):
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-canary-movie"}')

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "FAIL: Radarr has 0 movies\n"

        mock_get = MagicMock()
        resp = MagicMock(); resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", return_value=fake_result), \
             patch("lib.cli.requests.get", mock_get):
            rc = cli.main(["canary", "push", "movie"], manifest_path=None,
                          _manifest=manifest)

        assert rc == 2
        params = mock_get.call_args[1]["params"]
        assert params["status"] == "down"
        assert "FAIL" in params["msg"]

    def test_canary_push_failure_includes_stderr_in_msg(self, tmp_path, monkeypatch):
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-canary-movie"}')

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")

        fake_result = MagicMock()
        fake_result.returncode = 1
        fake_result.stdout = ""
        fake_result.stderr = "FAIL: specific error detail here\n"

        mock_get = MagicMock()
        resp = MagicMock(); resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", return_value=fake_result), \
             patch("lib.cli.requests.get", mock_get):
            cli.main(["canary", "push", "movie"], manifest_path=None,
                     _manifest=manifest)

        params = mock_get.call_args[1]["params"]
        assert "specific error detail here" in params["msg"]


class TestCanaryPushMissingScript:
    """canary push with missing script file → reports clear error, exits 1."""

    def test_canary_push_missing_script_exits_1(self, tmp_path, monkeypatch, capsys):
        from lib import cli

        manifest = _make_manifest_with_canary(script="/nonexistent/canary.sh")
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-canary-movie"}')

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")

        import subprocess
        fake_result = MagicMock()
        fake_result.returncode = 127
        fake_result.stdout = ""
        fake_result.stderr = "bash: /nonexistent/canary.sh: No such file or directory\n"

        mock_get = MagicMock()
        resp = MagicMock(); resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", return_value=fake_result), \
             patch("lib.cli.requests.get", mock_get):
            rc = cli.main(["canary", "push", "movie"], manifest_path=None,
                          _manifest=manifest)

        assert rc == 2
        params = mock_get.call_args[1]["params"]
        assert params["status"] == "down"


class TestCanaryPushUnknownCanary:
    """canary push with unknown canary name → exits 1 with error message."""

    def test_canary_push_unknown_name_exits_1(self, tmp_path, monkeypatch, capsys):
        from lib import cli

        manifest = _make_manifest_with_canary()

        rc = cli.main(["canary", "push", "nonexistent"], manifest_path=None,
                      _manifest=manifest)

        assert rc == 1
        err = capsys.readouterr().err
        assert "nonexistent" in err


class TestCanaryPushMissingToken:
    """canary push when no token present → status=down pushed without crashing."""

    def test_canary_push_no_token_skips_push(self, tmp_path, monkeypatch, capsys):
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{}')  # no token for canary-movie

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "PASS: movie canary\n"
        fake_result.stderr = ""

        mock_get = MagicMock()

        with patch("lib.cli.subprocess.run", return_value=fake_result), \
             patch("lib.cli.requests.get", mock_get):
            rc = cli.main(["canary", "push", "movie"], manifest_path=None,
                          _manifest=manifest)

        assert rc == 0
        mock_get.assert_not_called()


class TestCanaryPushDuringMaintenanceWindow:
    """While the maintenance-window lock is present, a canary must push UP
    [maint-window] and NOT run its (often heavyweight) script — apps are being
    upgraded, so a normal canary run would false-alarm. Same treatment the
    pusher gives apps during the window."""

    def test_canary_push_skips_script_and_pushes_up_in_window(self, tmp_path, monkeypatch):
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-canary-movie"}')
        (tmp_path / "lock").write_text("1\n2026-06-30T11:00:00Z\n")  # window lock present

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))

        mock_run = MagicMock()
        mock_get = MagicMock()
        resp = MagicMock(); resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", mock_run), \
             patch("lib.cli.requests.get", mock_get):
            rc = cli.main(["canary", "push", "movie"], manifest_path=None,
                          _manifest=manifest)

        assert rc == 0
        mock_run.assert_not_called()  # script must NOT run during the window
        assert mock_get.called
        params = mock_get.call_args[1]["params"]
        assert params["status"] == "up"
        assert "maint-window" in params["msg"]

    def test_canary_push_runs_normally_outside_window(self, tmp_path, monkeypatch):
        """Sanity: with no lock, the canary script runs and its result is pushed."""
        from lib import cli

        manifest = _make_manifest_with_canary()
        tokens_file = tmp_path / "tokens.json"
        tokens_file.write_text('{"canary-movie": "tok-canary-movie"}')

        monkeypatch.setenv("MANITOBA_KUMA_TOKENS", str(tokens_file))
        monkeypatch.setenv("MANITOBA_KUMA_URL", "http://127.0.0.1:42005")
        monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))  # no lock file

        fake_result = MagicMock()
        fake_result.returncode = 0
        fake_result.stdout = "PASS: movie canary\n"
        fake_result.stderr = ""
        mock_get = MagicMock()
        resp = MagicMock(); resp.status_code = 200
        mock_get.return_value = resp

        with patch("lib.cli.subprocess.run", return_value=fake_result) as mock_run, \
             patch("lib.cli.requests.get", mock_get):
            rc = cli.main(["canary", "push", "movie"], manifest_path=None,
                          _manifest=manifest)

        assert rc == 0
        mock_run.assert_called_once()  # script DID run outside the window
        params = mock_get.call_args[1]["params"]
        assert params["status"] == "up"
        assert "maint-window" not in params["msg"]


# ---------------------------------------------------------------------------
# Kuma audit includes canary monitors
# ---------------------------------------------------------------------------

class TestAuditIncludesCanaries:
    def test_audit_includes_canary_monitors(self, monkeypatch):
        from lib.kuma import audit_monitors
        from lib.manifest import App, HealthConfig, Manifest, Canary

        app = App(name="sonarr", class_="ucc", kuma_monitor="Sonarr",
                  health=HealthConfig(kind="http_root", raw={}), defaults={})
        canary = Canary(name="movie", kuma_monitor="Canary Movie",
                        script="scripts/canaries/movie.sh", schedule="hourly")
        m = Manifest({"sonarr": app}, {"movie": canary})

        metrics = (
            'monitor_status{monitor_name="Sonarr"} 1\n'
            'monitor_status{monitor_name="Canary Movie"} 1\n'
            'monitor_status{monitor_name="Manitoba Pusher"} 1\n'
            'monitor_status{monitor_name="QFlix Fleet"} 1\n'
            'monitor_status{monitor_name="QFlix Reaper"} 1\n'
        )
        resp = MagicMock(text=metrics)
        monkeypatch.setattr("lib.kuma.requests.get", lambda *a, **k: resp)
        monkeypatch.setattr("lib.kuma._secret_read", lambda n: "fake-key")

        report = audit_monitors(m, kuma_url="http://x")
        assert "Canary Movie" in report["matched"]
        # sonarr (1) + canary (1) + auto-injected "Manitoba Pusher" (1) + "QFlix Fleet" (1) + "QFlix Reaper" (1).
        assert report["manifest_count"] == 5

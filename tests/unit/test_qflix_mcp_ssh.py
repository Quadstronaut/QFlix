"""Tests for scripts/local/qflix-mcp/lib/ssh.py."""
from __future__ import annotations
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch, MagicMock

HERE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HERE / "scripts" / "local" / "qflix-mcp"))

from lib.ssh import ssh_call  # noqa: E402


@patch("subprocess.run")
def test_ssh_call_builds_cmd(mock_run, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "seedbox.ssh-host").write_text("manitoba.example.com")
    fake = MagicMock()
    fake.returncode = 0
    fake.stdout = '{"ok": true}'
    fake.stderr = ""
    mock_run.return_value = fake
    result = ssh_call("python3 ~/scripts/mcp/collect.py --emit-json",
                      secrets_dir=secrets, timeout=10)
    assert result.returncode == 0
    args = mock_run.call_args[0][0]
    assert args[0] == "ssh"
    assert "manitoba.example.com" in args[-2]  # user@host is second-to-last
    assert "python3 ~/scripts/mcp/collect.py --emit-json" in args


@patch("subprocess.run")
def test_ssh_call_propagates_nonzero(mock_run, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "seedbox.ssh-host").write_text("h")
    fake = MagicMock()
    fake.returncode = 5
    fake.stdout = ""
    fake.stderr = "err"
    mock_run.return_value = fake
    result = ssh_call("false", secrets_dir=secrets)
    assert result.returncode == 5
    assert result.stderr == "err"


@patch("subprocess.run")
def test_ssh_call_returns_124_on_timeout(mock_run, tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "seedbox.ssh-host").write_text("h")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["ssh"], timeout=5)
    result = ssh_call("sleep 999", secrets_dir=secrets, timeout=5)
    assert result.returncode == 124
    assert "ssh-timeout" in result.stderr
    assert "5" in result.stderr
    assert result.stdout == ""

"""SSH wrapper for invoking seedbox scripts. Reads SSH host from secrets/seedbox.ssh-host.

User is hardcoded to 'quadstronaut' (matches existing tunnel daemon convention).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional


def _read(p: Path) -> str:
    try:
        return p.read_text().strip()
    except FileNotFoundError:
        return ""


def _resolve_secrets_dir(override: Optional[Path]) -> Path:
    if override:
        return override
    env = os.environ.get("QFLIX_SECRETS_DIR")
    if env:
        return Path(env)
    # Default: walk up from this file to find repo root, then secrets/
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "secrets" / "seedbox.ssh-host"
        if candidate.exists():
            return parent / "secrets"
    return Path.home() / ".qflix" / "secrets"


def ssh_call(remote_cmd: str, *, secrets_dir: Optional[Path] = None,
             timeout: int = 30, user: str = "quadstronaut") -> subprocess.CompletedProcess:
    secrets = _resolve_secrets_dir(secrets_dir)
    host = _read(secrets / "seedbox.ssh-host")
    if not host:
        return subprocess.CompletedProcess(
            args=["ssh"], returncode=2,
            stdout="", stderr="seedbox.ssh-host not found",
        )
    cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15",
        f"{user}@{host}",
        remote_cmd,
    ]
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd, returncode=124,
            stdout="", stderr=f"ssh-timeout after {timeout}s",
        )

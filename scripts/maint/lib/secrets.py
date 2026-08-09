"""lib/secrets.py — single source of truth for the secrets directory.

Resolution order:
  1. MANITOBA_SECRETS_DIR (canonical env var)
  2. MANITOBA_SECRETS (back-compat for qbit.py, kept to avoid breaking
     prod overrides that may already exist on the seedbox)
  3. <repo>/secrets/ if running from a checkout (file at
     <repo>/scripts/maint/lib/secrets.py — repo root is 4 parents up)
  4. ~/secrets/ — the seedbox-canonical location

Prior to this module, four lib/*.py files each rolled their own copy of
this logic; one of them (qbit.py) used a different env var name, which
meant `export MANITOBA_SECRETS=...` only affected qbit-related ops and
silently diverged from the rest of the maint daemon. See the audit notes
for the diagnostic burden that bug created.
"""
from __future__ import annotations

import os
from pathlib import Path


def secrets_dir() -> Path:
    """Return the directory holding QFlix secret files. Never raises."""
    env = os.environ.get("MANITOBA_SECRETS_DIR") or os.environ.get("MANITOBA_SECRETS")
    if env:
        return Path(env).expanduser()
    # Repo-style layout: scripts/maint/lib/secrets.py → repo/secrets/
    repo_root_guess = Path(__file__).resolve().parent.parent.parent.parent
    repo_secrets = repo_root_guess / "secrets"
    if repo_secrets.is_dir():
        return repo_secrets
    return Path.home() / "secrets"


def read_secret(name: str) -> str:
    """Read and strip ~/secrets/<name>. Raises FileNotFoundError on missing
    so callers can decide whether to fall back or fail loudly."""
    path = secrets_dir() / name
    return path.read_text(encoding="utf-8").strip()

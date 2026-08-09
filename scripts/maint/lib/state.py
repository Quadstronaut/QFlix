"""lib/state.py — atomic state.json read/write.

Atomic writes use tempfile + os.replace() (atomic on POSIX).
Reads are fault-tolerant: missing or corrupt files return {}.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def read(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("state file root is not a JSON object")
        return data
    except Exception as exc:
        print(f"WARNING: state read failed for {path}: {exc}", file=sys.stderr)
        return {}


def write(path: str | Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(
        dir=path.parent,
        suffix=".tmp",
        prefix=path.name + ".",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        if os.name == "posix":
            os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def record(path: str | Path, app: str, **fields) -> None:
    data = read(path)
    if "apps" not in data or not isinstance(data["apps"], dict):
        data["apps"] = {}
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = {"updated_at": now, **fields}
    data["apps"][app] = entry
    write(path, data)

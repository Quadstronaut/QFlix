"""Read/write `B:\\QFlix\\data\\` snapshots, logs, events.

The MCP read tools operate against this cache exclusively (zero seedbox traffic).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, default=str)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> Any:
    """Read JSON tolerating UTF-8 BOM (PowerShell Out-File on PS 5.1 writes one
    by default even with -Encoding utf8)."""
    return json.loads(path.read_text(encoding="utf-8-sig"))


class Cache:
    def __init__(self, root: Path):
        self.root = Path(root)

    @property
    def snapshots_root(self) -> Path:
        return self.root / "snapshots"

    @property
    def logs_root(self) -> Path:
        return self.root / "logs"

    @property
    def events_root(self) -> Path:
        return self.root / "events"

    def _all_snapshot_files(self, hours: int = 720) -> list[Path]:
        """Return snapshot paths sorted oldest→newest, capped to last `hours`."""
        if not self.snapshots_root.exists():
            return []
        files = []
        for date_dir in sorted(self.snapshots_root.iterdir()):
            if not date_dir.is_dir():
                continue
            for f in sorted(date_dir.glob("*.json")):
                files.append(f)
        # Trim to last N hours by file count (one file per hour expected)
        return files[-hours:]

    def latest_snapshot(self) -> Optional[dict]:
        files = self._all_snapshot_files(hours=2)
        if not files:
            return None
        return _read_json(files[-1])

    def previous_snapshots(self, n: int) -> list[dict]:
        """Returns the n snapshots immediately *before* the latest, newest first."""
        files = self._all_snapshot_files(hours=n + 1)
        if len(files) <= 1:
            return []
        # Drop latest, return up to n preceding, newest-first
        prev = list(reversed(files[:-1]))[:n]
        return [_read_json(f) for f in prev]

    def snapshots_in_range(self, hours: int) -> list[dict]:
        files = self._all_snapshot_files(hours=hours)
        return [_read_json(f) for f in files]

    def history_for_hash(self, hash_: str, *, hours: int = 24) -> list[dict]:
        out = []
        for snap in self.snapshots_in_range(hours):
            for t in (snap.get("qbit", {}).get("torrents") or []):
                if (t.get("hash") or "").lower() == hash_.lower():
                    out.append({
                        "captured_at": snap.get("captured_at"),
                        "downloaded_bytes": t.get("downloaded_bytes"),
                        "progress": t.get("progress"),
                        "dl_speed_bytes_s": t.get("dl_speed_bytes_s"),
                        "state": t.get("state"),
                    })
        return out

    def write_snapshot(self, dt_utc: dt.datetime, content: dict) -> Path:
        date = dt_utc.strftime("%Y-%m-%d")
        path = self.snapshots_root / date / f"{dt_utc.strftime('%H')}.json"
        atomic_write_json(path, content)
        return path

    def stale_state_path(self) -> Path:
        return self.root / "stale-state.json"

    def load_stale_state(self) -> dict:
        p = self.stale_state_path()
        if not p.exists():
            return {"hashes": {}, "updated_at": None}
        try:
            return _read_json(p)
        except json.JSONDecodeError:
            return {"hashes": {}, "updated_at": None}

    def save_stale_state(self, state: dict) -> None:
        atomic_write_json(self.stale_state_path(), state)

    def append_event(self, line: dict) -> None:
        self.events_root.mkdir(parents=True, exist_ok=True)
        date = (line.get("ts") or dt.datetime.now(dt.timezone.utc).isoformat())[:10]
        with (self.events_root / f"{date}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, default=str) + "\n")

    def recent_events(self, n: int = 50) -> list[dict]:
        if not self.events_root.exists():
            return []
        out = []
        for f in sorted(self.events_root.glob("*.jsonl"), reverse=True):
            for line in reversed(f.read_text().splitlines()):
                if line.strip():
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if len(out) >= n:
                        return out
        return out

#!/usr/bin/env python3
"""arr-housekeeping — daily Find-Missing sweep + hourly stuck-queue unstick.

Two modes:
  --missing   Fire MissingSearch command on each *arr. Sched: 04:00 Tue–Sun
              (Monday is the cp.ultra.cc maintenance window; we skip it).
  --unstick   Scan each *arr's queue for items stuck in completed-but-not-
              imported state (importPending / importBlocked / importFailed)
              for >=STUCK_HOURS. For each, DELETE the queue item with
              removeFromClient=true and blocklist=true — Sonarr/Radarr
              auto-search a replacement after the blocklist add. Sched:
              hourly.

State for stuck-tracking is keyed by qBit downloadId (hash) so it's stable
across queue-id renumberings. Stored at ~/.opt/maint/stuck-queue-state.json.

Reads creds from ~/secrets/{arr}.key + ~/secrets/{arr}.urlbase + the shared
htpasswd password. Posts a Discord summary via lib.notify on completion.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

# Public host comes from secrets/seedbox.host (gitignored) — die loudly
# rather than silently hitting the sanitized placeholder if it's missing.
# Override via ARR_HOST env for tests.
def _resolve_host() -> str:
    env = os.environ.get("ARR_HOST")
    if env:
        return env
    try:
        fqdn = Path(os.environ.get("MANITOBA_SECRETS",
                                   str(Path.home() / "secrets"))).joinpath(
            "seedbox.host").read_text(encoding="utf-8").strip()
        return f"https://{fqdn}" if fqdn else ""
    except FileNotFoundError:
        return ""

HOST = _resolve_host()
SECRETS_DIR = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))
STATE_DIR = Path(os.environ.get("MANITOBA_STATE_DIR", str(Path.home() / ".opt" / "maint")))
STUCK_STATE_FILE = STATE_DIR / "stuck-queue-state.json"

STUCK_HOURS = float(os.environ.get("ARR_STUCK_HOURS", "6"))
STUCK_STATES = {"importPending", "importBlocked", "importFailed"}

# (slug, api version, missing-search command name)
# Readarr removed 2026-05-16 — app purged 2026-05-11; secret_read on its
# .key file dies, hidden by _read()'s try/except (returns "") so the loop
# silently skipped Readarr anyway. Drop the entry for honesty.
ARRS = [
    ("sonarr",   "v3", "MissingEpisodeSearch"),
    ("sonarr2",  "v3", "MissingEpisodeSearch"),
    ("radarr",   "v3", "MissingMoviesSearch"),
    ("radarr2",  "v3", "MissingMoviesSearch"),
]


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""


# Read lazily inside _basic() rather than at import time so the absence of
# secrets/htpasswd.password is reported by the first request (with a clear
# 401), not silently propagated as an empty Basic header that every *arr
# then 401s on while the script reports the requests as "skip".
def _htpw() -> str:
    pw = _read(SECRETS_DIR / "htpasswd.password")
    if not pw:
        raise RuntimeError(
            "secrets/htpasswd.password missing or empty — refusing to "
            "issue unauthenticated *arr requests"
        )
    return pw


def _basic() -> str:
    return "Basic " + base64.b64encode(f"quadstronaut:{_htpw()}".encode()).decode()


def _hdr(api_key: str, *, json_body: bool = False) -> dict:
    h = {"X-Api-Key": api_key, "Authorization": _basic(), "Accept": "application/json"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _req(method: str, url: str, api_key: str, body: dict | None = None,
         timeout: int = 30) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                  headers=_hdr(api_key, json_body=body is not None))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode(errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="ignore")[:600]
    except Exception as e:
        return 0, str(e)[:300]


def _arr_url(slug: str, ver: str, path: str, query: str = "") -> str:
    urlbase = _read(SECRETS_DIR / f"{slug}.urlbase") or slug
    qs = f"?{query}" if query else ""
    return f"{HOST}/{urlbase}/api/{ver}/{path}{qs}"


def _arr_key(slug: str) -> str:
    return _read(SECRETS_DIR / f"{slug}.key")


def _notify(msg: str, level: str = "info") -> None:
    """Discord notification via lib.notify (Notifiarr was retired 2026-05-10).
    Best-effort; never raise. Adds the operator @ping for warning/error levels."""
    try:
        # Resolve the import path so this works both from a repo checkout
        # (scripts/maint/lib) and a seedbox deploy (~/scripts/maint/lib).
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        from lib.notify import notify  # type: ignore
        notify(msg, level)
    except Exception as exc:
        print(f"notify failed (non-fatal): {exc}", file=sys.stderr)


# ----- mode: --missing ----------------------------------------------------

def cmd_missing(dry_run: bool) -> int:
    """Delegates to scripts/mcp/missing.py to keep one source of truth."""
    if dry_run:
        print("--- find-missing sweep (DRY-RUN, delegated to mcp/missing.py) ---")
        return 0
    here = Path(__file__).resolve().parent
    mcp = here.parent / "mcp" / "missing.py"
    # Validate the helper exists before subprocess — the prior FileNotFoundError
    # surfaced only as a non-zero returncode in the systemd journal, with no
    # signal that the path layout was the cause.
    if not mcp.is_file():
        print(f"FATAL: missing helper not found at {mcp} — "
              f"layout drift between scripts/maint and scripts/mcp",
              file=sys.stderr)
        return 2
    proc = subprocess.run(
        ["python3", str(mcp), "--emit-json"],
        capture_output=True, text=True, timeout=120,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


# ----- mode: --unstick ----------------------------------------------------

def _load_state() -> dict:
    if not STUCK_STATE_FILE.exists():
        return {}
    try:
        return json.loads(STUCK_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STUCK_STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _is_stuck(item: dict) -> bool:
    """An item is 'stuck' if it's completed but the *arr hasn't imported it."""
    return (
        item.get("status") == "completed"
        and item.get("trackedDownloadState") in STUCK_STATES
    )


def _state_key(slug: str, download_id: str) -> str:
    return f"{slug}:{(download_id or 'no-hash').lower()}"


def cmd_unstick(dry_run: bool) -> int:
    print(f"--- unstick-queue sweep ({'DRY-RUN' if dry_run else 'LIVE'}) ---")
    state = _load_state()
    now = time.time()
    cutoff = now - (STUCK_HOURS * 3600)
    actions: list[str] = []
    new_state: dict = {}

    for slug, ver, _ in ARRS:
        key = _arr_key(slug)
        if not key:
            continue
        url = _arr_url(slug, ver, "queue", query="pageSize=500&includeUnknownSeriesItems=true")
        code, body = _req("GET", url, key)
        if code != 200:
            print(f"  ! {slug}: GET queue HTTP {code}")
            continue
        try:
            payload = json.loads(body)
        except Exception:
            print(f"  ! {slug}: queue body parse fail")
            continue
        records = payload.get("records") if isinstance(payload, dict) else payload
        if not records:
            continue

        for item in records:
            if not _is_stuck(item):
                continue
            sk = _state_key(slug, item.get("downloadId", ""))
            qid = item.get("id")
            title = (item.get("title") or "?")[:80]

            prev = state.get(sk)
            if prev is None:
                # First time seeing this stuck item — record + carry forward.
                new_state[sk] = {
                    "title": title,
                    "queue_id": qid,
                    "first_seen_stuck": now,
                    "slug": slug,
                }
                continue

            first_seen = float(prev.get("first_seen_stuck", now))
            age_hours = (now - first_seen) / 3600
            if first_seen >= cutoff:
                # Still stuck but hasn't aged out yet — carry forward.
                new_state[sk] = prev
                continue

            # Aged out: remove from client + blocklist. Sonarr/Radarr's
            # default behavior is to re-search after a blocklist add; we
            # don't pass skipRedownload so the *arr does that for us.
            del_url = _arr_url(
                slug, ver, f"queue/{qid}",
                query="removeFromClient=true&blocklist=true",
            )
            if dry_run:
                msg = f"  [dry-run] {slug}: would unstick (id={qid}, age={age_hours:.1f}h) -> {title}"
                print(msg)
                actions.append(f"DRY {slug}: {title} ({age_hours:.1f}h)")
                # Carry-forward in dry-run too so the second pass doesn't double-count
                new_state[sk] = prev
                continue

            dcode, dbody = _req("DELETE", del_url, key)
            if dcode in (200, 204):
                msg = f"  ✓ {slug}: unstuck id={qid} age={age_hours:.1f}h — {title}"
                print(msg)
                actions.append(f"{slug}: {title} ({age_hours:.1f}h) → blocklisted+research")
                # Don't carry-forward — once removed, this hash is gone.
            else:
                print(f"  ! {slug}: DELETE id={qid} HTTP {dcode}: {dbody[:200]}")
                # Keep in state so we'll retry next run.
                new_state[sk] = prev

    _save_state(new_state)

    print(
        f"\nstuck items still tracked (carrying forward): {len(new_state)}, "
        f"actions taken: {len(actions)}"
    )
    if actions:
        _notify(
            "arr-unstick swept:\n" + "\n".join(actions),
            level="warning" if len(actions) > 0 else "info",
        )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--missing", action="store_true",
                   help="trigger MissingSearch command on each *arr")
    g.add_argument("--unstick", action="store_true",
                   help="DELETE+blocklist queue items stuck >=STUCK_HOURS")
    ap.add_argument("--dry-run", action="store_true",
                    help="show planned actions without executing")
    args = ap.parse_args()

    if args.missing:
        return cmd_missing(args.dry_run)
    if args.unstick:
        return cmd_unstick(args.dry_run)
    return 1


if __name__ == "__main__":
    sys.exit(main())

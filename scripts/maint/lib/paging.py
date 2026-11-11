"""lib/paging.py - QFLX-123: which signals page Discord NOW, and where the rest go.

Operator law (2026-11-11): "qflix to basically be silent and stable, with weekly
notifications and checks." Only a MEMBER-FACING failure pages Discord
immediately. Everything else keeps running, keeps its Kuma state and its
auto-heal, and lands in a weekly roll-up instead of the alerts channel.

ONE place decides membership: the top-level `paging:` mapping in
manifest/apps.yaml, `{<Kuma monitor name>: <one-line reason>}`. Every path that
can reach Discord keys off those names:

  * Kuma channel 1 ("Mission Control - QFlix") is attached to exactly those
    monitors by bootstrap-kuma-monitors.py; channel 2 (auto-heal webhook) stays
    on every monitor. qflix-audit-live.py L-09 flags drift either way.
  * lib.notify.notify(..., source=<monitor name>) pages only when `source` is in
    the set. Untagged calls never page: membership is opt-in, default quiet.
  * Everything that did NOT page is appended to WEEKLY_EVENTS (one JSON line per
    suppressed notification) for the Monday summary to count.

Fail-OPEN on the decision that matters: if the manifest cannot be read, a TAGGED
notify pages anyway (alerting-needs-cross-run-dedup law: an unreadable ledger
must never eat a real page). An UNTAGGED notify stays quiet regardless - it was
never declared member-facing, so no manifest state can make it one.

Stdlib + pyyaml only, and no import of lib.manifest / lib.notify: both of those
import THIS module, and notify() runs inside every janitor on the box.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Kuma notification channels, matched by NAME (ids differ per Kuma instance -
# a migrated box gets fresh ids, names survive). Same strings the bootstrap and
# scripts/migrate/kuma_channels.py use.
DISCORD_CHANNEL_NAME = "Mission Control - QFlix"
AUTOHEAL_CHANNEL_NAME = "Manitoba auto-heal webhook"

# Roll-up input for the weekly summary, under MANITOBA_STATE_DIR (~/.opt/maint).
WEEKLY_EVENTS = "weekly-events.jsonl"

# Rotation: rewrite to the newest _WEEKLY_KEEP_LINES once the file passes
# _WEEKLY_MAX_BYTES. At the measured ~4 Discord sends/day plus every Kuma
# DOWN/UP edge, 20k lines is months of history - far past the 7 days the
# weekly reader needs - while the byte cap keeps the stat-per-append cheap.
_WEEKLY_MAX_BYTES = 4 * 1024 * 1024
_WEEKLY_KEEP_LINES = 20000

_MSG_MAX = 160
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


# ---------------------------------------------------------------------------
# The member-facing set
# ---------------------------------------------------------------------------

def parse(raw) -> dict:
    """Validate the manifest's `paging:` block -> {monitor name: reason}.

    Raises ValueError on a malformed block. A reason is REQUIRED: the operator's
    rule is "justify each inclusion in one line", and an entry nobody can
    justify is exactly the drift that turns the pager back into noise."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("'paging' must be a mapping of {Kuma monitor name: reason}")
    out = {}
    for name, why in raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("paging: monitor names must be non-empty strings")
        if not isinstance(why, str) or not why.strip():
            raise ValueError("paging: '%s' needs a one-line reason" % name)
        out[name] = why.strip()
    return out


def manifest_path() -> Path:
    """Same precedence the box daemons use: explicit env, then the DEPLOYED
    manifest, then the repo copy (workstation runs and unit tests)."""
    for var in ("MANITOBA_MANIFEST_PATH", "MANITOBA_MANIFEST"):
        val = os.environ.get(var)
        if val:
            return Path(val).expanduser()
    deployed = Path.home() / ".opt" / "maint" / "apps.yaml"
    if deployed.is_file():
        return deployed
    return Path(__file__).resolve().parents[3] / "manifest" / "apps.yaml"


_cache: dict = {}


def _load(path: Optional[Path] = None) -> Optional[dict]:
    """Raw manifest mapping, cached per (path, mtime). None if unreadable."""
    p = Path(path) if path else manifest_path()
    try:
        mtime = p.stat().st_mtime_ns
    except OSError:
        return None
    hit = _cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        import yaml  # noqa: PLC0415 - optional at import time on purpose
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("paging: manifest unreadable (%s): %r\n" % (p, exc))
        return None
    if not isinstance(data, dict):
        return None
    _cache[str(p)] = (mtime, data)
    return data


def paging_monitors(path: Optional[Path] = None) -> Optional[dict]:
    """{monitor: reason} for the member-facing set, or None when it cannot be
    determined (manifest missing/unparseable/invalid block)."""
    data = _load(path)
    if data is None:
        return None
    try:
        return parse(data.get("paging"))
    except ValueError as exc:
        sys.stderr.write("paging: invalid paging block: %s\n" % exc)
        return None


def external_monitors(path: Optional[Path] = None) -> set:
    data = _load(path) or {}
    return {str(m) for m in (data.get("kuma_external_monitors") or [])}


def monitor_for_app(app_name: str, path: Optional[Path] = None) -> Optional[str]:
    """The Kuma monitor an app pushes to - the `source` its recovery pages under."""
    data = _load(path) or {}
    app = (data.get("apps") or {}).get(app_name)
    if isinstance(app, dict):
        return app.get("kuma_monitor") or None
    return None


def should_page(source: Optional[str]) -> bool:
    """True iff a notification tagged `source` pages Discord immediately."""
    if not source:
        return False
    members = paging_monitors()
    if members is None:
        return True        # fail OPEN: a tagged page is never eaten by a read error
    return source in members


# ---------------------------------------------------------------------------
# The weekly roll-up input
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    env = os.environ.get("MANITOBA_STATE_DIR")
    return Path(env) if env else Path.home() / ".opt" / "maint"


def weekly_events_path() -> Path:
    return _state_dir() / WEEKLY_EVENTS


def scrub(text: object, limit: int = _MSG_MAX) -> str:
    """First line only, e-mail addresses masked, clipped.

    The roll-up is read off the box and summarised into Discord; per-member
    detail must never ride along (never-publish-member-data). The first line is
    the headline every sender here leads with; the rest is triage detail that
    stays in notify.log on the box."""
    first = str(text or "").strip().splitlines()[0] if str(text or "").strip() else ""
    first = _EMAIL_RE.sub("<email>", first)
    return first[:limit]


@contextlib.contextmanager
def _locked(directory: Path):
    """Exclusive flock on a sidecar so an append can never land in the old
    inode while a rotation rewrites the file (shared-file-rmw law). No fcntl
    (Windows test runs) means no lock - single-process there."""
    try:
        import fcntl  # noqa: PLC0415
    except ImportError:
        yield
        return
    with open(directory / (WEEKLY_EVENTS + ".lock"), "a", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _rotate(path: Path) -> None:
    try:
        if path.stat().st_size <= _WEEKLY_MAX_BYTES:
            return
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(lines[-_WEEKLY_KEEP_LINES:]), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        sys.stderr.write("paging: weekly-events rotation failed (continuing): %r\n" % exc)


def record_weekly(kind: str, monitor: Optional[str], state: str, msg: object,
                  *, now: Optional[datetime] = None) -> bool:
    """Append ONE suppressed notification to WEEKLY_EVENTS. Never raises.

    Line format (JSON, one object per line, keys sorted):
      ts      ISO-8601 UTC, "YYYY-MM-DDTHH:MM:SSZ"
      kind    "kuma"   - a Kuma DOWN/UP edge on a non-paging monitor
                         (seen via the auto-heal webhook, channel 2)
              "notify" - a lib.notify call that was not member-facing
      monitor Kuma monitor name, or null for an untagged notify
      origin  the emitting program (argv[0] basename), for grouping
      state   kuma: "down" | "up"; notify: the level ("info"/"warning"/...)
      msg     <=160 chars, first line only, e-mail addresses masked
    json.dumps escapes every newline, so a hostile release title can never
    forge a second record (the CWE-117 class notify.log guards by hand)."""
    try:
        ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        rec = {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kind": kind,
            "monitor": monitor or None,
            "origin": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else None,
            "state": str(state),
            "msg": scrub(msg),
        }
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / WEEKLY_EVENTS
        line = json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n"
        with _locked(directory):
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
            _rotate(path)
        return True
    except Exception as exc:                                   # noqa: BLE001
        sys.stderr.write("paging: could not record weekly event: %r\n" % exc)
        return False

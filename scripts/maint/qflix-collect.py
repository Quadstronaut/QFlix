#!/usr/bin/env python3
"""scripts/maint/qflix-collect.py — seedbox-side hourly farm collector.

Always-on Python port of the workstation orchestrator scripts/local/
qflix-collect.ps1. Migrated off the operator PC ("devil") to the qflix box
on 2026-07-09 because the workstation-resident job left the Kuma monitor
"QFlix Collect (seedbox)" red — a CUSTOMER-VISIBLE false failure on the
public status page — whenever the PC was off, and silently stopped the
autonomous unstick loop. Same reasoning that moved VLogs ingest to the box
on 2026-05-14 (the autonomy mandate: no autonomy-critical job may depend on
the operator's PC being on).

Runs each hour under systemd-user timer `qflix-collect.timer`. Since it runs
ON the box, the SSH hops the PowerShell version made collapse into local
subprocess calls to ~/scripts/mcp/{collect,logs,unstick}.py.

Flow (mirrors the PS script's box-relevant steps):
  1. flock single-instance lock.
  2. collect.py --emit-json  -> snapshots/<date>/HH.json
  3. logs.py   --emit-json  -> logs/<date>/<app>.log (append)
  4. Walk last 3 snapshots -> stale-state.json; select unstick candidates.
  5. unstick.py per candidate (cap 10/day) -> events/<date>.jsonl.
  6. Discord summary + Kuma push (dead-man heartbeat).
  7. last-collect.json; prune retention.

Data root defaults to ~/.opt/qflix-collect (override QFLIX_COLLECT_DATA).
Exit 0 on success, 1 on fatal error (a down push to Kuma precedes it).

LOG-COVERAGE LEDGER (added 2026-08-19)
--------------------------------------
Step 3 wrote one logs/<date>/<app>.log per app that returned lines, and
`if not lines: continue` for everyone else. That `continue` is the whole
defect: an app that stops producing lines simply STOPS EXISTING in the
snapshot dir, with no log line, no Discord post, no Kuma msg, nothing.

Measured on the box (read-only) 2026-08-19/20:
  logs/2026-08-17/  listmonk.log ABSENT
  logs/2026-08-18/  listmonk.log present, 994783 B, last written 10:01 CEST
  logs/2026-08-19/  listmonk.log ABSENT
  logs/2026-08-20/  listmonk.log ABSENT
`systemctl --user is-active listmonk` = active, SubState=running,
NRestarts=36, ExecMainStartTimestamp = Tue 2026-08-18 09:53:11 CEST, and
`journalctl --user -u listmonk.service --since "2026-08-18 09:53:12"` is
"-- No entries --" against 5793 lines lifetime. So the Aug-18 host reboot
drove a 36-restart storm that filled logs/2026-08-18/listmonk.log, and
listmonk has said nothing since: it is a journal-QUIET app, an hourly
`--since 1h` window is legitimately empty, and it fell out of the dailies.

The absence is NOT logs.py forgetting it. `logs.py --app all --emit-json`
run live on the box returns 21 keys including
  listmonk      journalctl:listmonk.service   0 lines  error=None
  maint-window  journalctl:manitoba-maint-window.service  0 lines
  tdarr-server  journalctl:tdarr-server.service           0 lines
so the routing table is intact and the app is present in the payload with
an empty `lines`. It is qflix-collect.py's own `if not lines: continue`
that turns "present and quiet" into "no file on disk". maint-window
(Mondays only) and tdarr-server (start/stop only) drop out the same way.

CORRECTION 2026-08-23: "the routing table is intact" above was WRONG, and the
ledger this module added is what proved it. listmonk, tdarr-server and
tdarr-node set StandardOutput=append:<file>, so journalctl never held their
stdout at all -- only systemd's own Started/Stopped lines. They were not
"journal-quiet apps"; they were misrouted, and their real logs (~2.5 MB/day for
tdarr) were never collected. Fixed in scripts/mcp/logs.py by moving all three to
_FILE_LOGS. maint-window below is the genuine article: it really is
StandardOutput=journal and really does only run on Mondays.

That re-route alone would have been a downgrade, not a fix. This module grades
an app on `len(lines)`, and logs.collect_for's file branch used to ignore
`--since` entirely -- so a file-routed app read LIVE for as long as its log
file existed and was non-empty, which for an append-only file systemd never
truncates is forever. Moving three apps onto that branch would have swapped a
permanent false DARK for a permanent false LIVE. collect_for now applies the
window to file routes too (logs._file_is_dormant), so `dark` still means what
it says: nothing written inside the window.

NGINX IS PERMANENTLY DARK, AND THAT IS THE CORRECT ANSWER. After that routing
fix `logs-dark` fell from five sources to one: nginx. Measured 2026-08-24 --
`~/.apps/nginx/logs/error.log` is 0 bytes, and `access.log` has been 0 bytes
since 2026-05-08. The route is not broken: `error.log.1` holds 593 bytes from the
2026-08-20 rotation, so this IS the file nginx writes and rotation works. It is
empty because there have been no nginx errors since, and access logging is off on
this panel-managed slot. An empty error log is the healthy state for a reverse
proxy, so this source reads dark by design and will keep doing so. Do not "fix"
it by re-pointing the route, and do not exempt it either -- `dark` never reds,
and a truthful footnote is worth more than a silent exemption.

So listmonk was healthy. That is exactly what makes it dangerous: the
collector rendered "healthy and silent" and "gone" as the same thing --
byte-identical absence. A renamed systemd unit, a rotated-away log path,
or an app dropped from logs.py's routing tables all look like a quiet
Tuesday. Nothing on the box compares yesterday's app set to today's:
canaries/stale-log-watchdog.sh watches 5 hand-listed SOURCE logs, not the
19 apps the collector covers.

The fix keeps the empty-file behaviour (writing zero-byte logs would just
move the noise) and instead keeps a ledger of who has ever produced lines,
graded three ways per cycle:

  roster-drop   an app in the ledger is ABSENT from logs.py's payload keys.
                logs.py builds those keys from static tables, so a missing
                key means the routing table changed -- unambiguous, pages.
  source-error  the payload entry carries an `error` (logs.py's
                {"app":..,"error":"unsupported","lines":[]} shape), which
                the old `if not lines: continue` swallowed whole. Pages.
  dark          present, but zero lines for longer than the app's own
                tolerance. Reported in the Kuma msg + journald, NOT a red:
                we genuinely cannot distinguish quiet-healthy from gone,
                and a red that cannot be cleared gets muted.

`dark` self-calibrates rather than using one global threshold, because a
fixed 26h would page on maint-window every non-Monday and get ignored
inside a week. The ledger records the longest gap each app has ever gone
between line-producing cycles; tolerance is max(floor, 2x that gap). A
chatty app (any *arr, plex) is called dark after ~a day; maint-window
widens its own tolerance to ~2 weeks the first Monday it runs.

EXPECTED ON FIRST DEPLOY: the ledger starts empty, so every bursty app is
called dark once, from ~26h in until it completes one full cadence and
calibrates itself (maint-window settles after its first Monday, ~1 week).
That is the bootstrap cost of measuring cadence instead of declaring it,
and it is a msg fragment, not a red. listmonk will keep reporting dark
until it emits again, which is the correct reading of the box's state.

RAIL, AND WHAT IT COSTS. Coverage rides the collector's EXISTING rails --
the Kuma msg, journald, Discord, last-collect.json -- rather than adding a
monitor. That means roster-drop / source-error push `down` to Kuma monitor
79 "QFlix Collect (seedbox)", which sits in the "Infrastructure &
Observability" group of the PUBLIC status page (verified in kuma.db
2026-08-20: status_page.slug=public, published=1). A roster drop therefore
shows customer-side until an operator fixes logs.py or names the app in
QFLIX_COLLECT_LOG_ROSTER_IGNORE. That is deliberate and it is why `dark`
is excluded from the red: a roster drop is unambiguous, actionable, and
CLEARABLE, while a dark app cannot be distinguished from a healthy quiet
one and would pin the public page red forever. Do not promote `dark` to a
red without first giving it a private monitor of its own.

Retiring an app is an explicit operator act, never an automatic decay: a
decommissioned app stays a roster-drop forever until it is named in
QFLIX_COLLECT_LOG_ROSTER_IGNORE (a systemd drop-in), same principle as
deploy-drift.sh's is_generated list -- an exemption that expires on its own
is a hiding place. See the books-stack purge (2026-08-16) for the shape of
change that needs it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# --- Config ---------------------------------------------------------------
DATA_ROOT = Path(os.environ.get(
    "QFLIX_COLLECT_DATA", str(Path.home() / ".opt" / "qflix-collect")))
MCP_DIR = Path(os.environ.get(
    "QFLIX_MCP_DIR", str(Path.home() / "scripts" / "mcp")))
KUMA_BASE = os.environ.get("KUMA_BASE", "http://127.0.0.1:42005")
# The monitor was created workstation-side under this exact name; the box's
# ~/secrets/kuma-push-tokens.json already carries the token under this key,
# so the box feeds the SAME monitor — no Kuma re-creation needed.
KUMA_PUSH_KEY = os.environ.get("QFLIX_COLLECT_KUMA_KEY", "QFlix Collect (seedbox)")
MAX_ACTIONS_PER_DAY = int(os.environ.get("QFLIX_COLLECT_MAX_ACTIONS", "10"))

DEAD_SLOW_BYTES = 10000        # dl_speed below this on a downloading torrent = dead-slow
ZERO_MOVEMENT_HOURS = 3        # snapshots of zero downloaded-delta before acting
META_STUCK_AGE_S = 86400       # metaDL + size 0 must be >=24h old to act

# --- C3/C4: SAB (Usenet) stuck-handling parity (2026-07-19 spec) -----------
# SAB `Status` strings verbatim, per spec C2/C3. Duplicated here rather than
# imported from scripts/mcp/collect.py: this script only ever shells out to
# sibling MCP scripts via _run_mcp(), it never imports their modules --
# keeping that boundary means this file and collect.py stay independently
# editable (both are mid-flight in the same parallel build).
SAB_PAUSED_STATE = "Paused"
SAB_DOWNLOADISH_STATES = frozenset({
    "Downloading", "Queued", "Grabbing", "Fetching", "Propagating",
})
SAB_PP_STATES = frozenset({
    "Verifying", "Repairing", "Extracting", "Moving", "Running",
    "QuickCheck", "Checking",
})

def _env_int(name: str, default: int) -> int:
    """Parse an int env override, falling back to `default` on absence OR a
    malformed value. These run at import; a bare int(os.environ.get(...))
    would raise ValueError on e.g. PP_HUNG_ESCALATE_HOURS='' before main()'s
    try/except exists, killing the entire collect cycle (incl. the armed
    breaker) with no Kuma-down heartbeat — a silent, self-inflicted outage
    (council 2026-07-20, Defect 7). A typo'd knob must degrade to the
    default and warn, never crash the collector."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        # warn()/log() are defined below this import-time call site, so emit
        # directly to stderr rather than depend on definition order.
        print("[qflix-collect] WARNING: ignoring malformed {}={!r}, "
              "using default {}".format(name, raw, default),
              file=sys.stderr, flush=True)
        return default


# C4 escalation circuit-breaker knobs (env-overridable per spec).
PP_HUNG_ESCALATE_HOURS = _env_int("PP_HUNG_ESCALATE_HOURS", 4)
SAB_REPAIR_COOLDOWN_H = _env_int("SAB_REPAIR_COOLDOWN_H", 24)
# Delay before re-polling queue_meta() to verify a fired restart_repair (SAB
# restarts mid-response and needs time to come back up). Tests monkeypatch
# this to 0 rather than block on a real 60s sleep.
SAB_REPAIR_VERIFY_DELAY_S = _env_int("QFLIX_COLLECT_SAB_VERIFY_DELAY_S", 60)

# --- Log-coverage knobs (see the LOG-COVERAGE LEDGER note in the docstring) -
# Floor before a never-yet-calibrated app is called dark. 26h, not 24h: the
# timer is hourly and the dailies roll at midnight UTC, so a 24h floor would
# flag an app that produced lines in hour 00 yesterday and hour 01 today.
LOG_DARK_MIN_HOURS = _env_int("QFLIX_COLLECT_LOG_DARK_MIN_H", 26)
# Multiplier on the app's own longest observed quiet gap. 2x, matching the
# 1.5x-cadence convention in canaries/stale-log-watchdog.sh but wider, because
# the gap here is MEASURED (and therefore a lower bound) rather than declared.
LOG_DARK_GAP_MULT = _env_int("QFLIX_COLLECT_LOG_DARK_GAP_MULT", 2)
# Consecutive OBSERVED quiet cycles required before wall-clock silence counts.
# 3, the same evidence bar ZERO_MOVEMENT_HOURS sets for a stalled torrent: it
# stops a collector outage (timer down, box rebooted) from being reported as
# every app going dark at once on the first cycle back, when in truth the only
# thing we sampled was one `--since 1h` window.
LOG_DARK_MIN_CYCLES = _env_int("QFLIX_COLLECT_LOG_DARK_MIN_CYCLES", 3)
LOG_COVERAGE_FILE = "log-coverage.json"


# --- Logging (systemd routes stdout/stderr to journald) -------------------
def log(msg: str) -> None:
    print("[qflix-collect] " + msg, flush=True)


def warn(msg: str) -> None:
    print("[qflix-collect] WARNING: " + msg, file=sys.stderr, flush=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or utc_now()).isoformat().replace("+00:00", "Z")


# --- Best-effort notify + Kuma (never raise into main flow) ---------------
def _notify(msg: str, level: str = "info") -> None:
    """Discord via lib.notify (matches qflix-reaper). Degrades to a logged
    no-op if the dep/webhook is missing. Never raises."""
    try:
        if str(_HERE) not in sys.path:
            sys.path.insert(0, str(_HERE))
        from lib.notify import notify
        notify(msg, level)
    except ImportError as exc:
        warn("notify unavailable (missing dep), continuing: " + str(exc))
    except Exception as exc:
        warn("notify failed (non-fatal): " + str(exc))


def _read_kuma_token() -> str:
    env = os.environ.get("QFLIX_COLLECT_KUMA_TOKEN")
    if env:
        return env
    try:
        path = Path.home() / "secrets" / "kuma-push-tokens.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get(KUMA_PUSH_KEY, "") or ""
    except Exception:
        return ""


def _push_kuma(status: str, msg: str) -> None:
    """Push a heartbeat to Kuma (stdlib urllib GET). status 'up'|'down'.
    Best-effort; swallows all errors."""
    token = _read_kuma_token()
    if not token:
        warn("no Kuma push token for '" + KUMA_PUSH_KEY + "' — skipping push")
        return
    qs = urllib.parse.urlencode({"status": status, "msg": msg[:200], "ping": 0})
    url = KUMA_BASE + "/api/push/" + token + "?" + qs
    try:
        urllib.request.urlopen(url, timeout=8).read()
    except Exception as exc:
        warn("Kuma push failed (non-fatal): " + str(exc))


# --- Run-lock (flock; auto-releases on process exit) ----------------------
_LOCK_PATH = os.environ.get("QFLIX_COLLECT_LOCK", "/tmp/qflix-collect.lock")


def _acquire_run_lock():
    try:
        import fcntl
    except ImportError:
        return True
    try:
        fh = open(_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (OSError, IOError):
        return None


def _release_run_lock(handle) -> None:
    if handle is None or handle is True:
        return
    try:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception as _exc:
        sys.stderr.write("qflix-collect.py: run-lock release failed - run-lock degrades to a no-op: "
                         + repr(_exc) + "\n")
    try:
        handle.close()
    except Exception as _exc:
        sys.stderr.write("qflix-collect.py: run-lock file close failed (best-effort, continuing): "
                         + repr(_exc) + "\n")


# --- MCP subprocess helper ------------------------------------------------
def _run_mcp(script: str, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Invoke ~/scripts/mcp/<script> with args. The PowerShell version SSH'd
    these; on the box they are local subprocesses."""
    cmd = ["python3", str(MCP_DIR / script), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# --- Atomic JSON write ----------------------------------------------------
def _write_json_atomic(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    os.replace(tmp, path)   # atomic same-filesystem rename


# --- Step 2: snapshot -----------------------------------------------------
def collect_snapshot() -> Path:
    r = _run_mcp("collect.py",
                 ["--emit-json", "--include", "qbit,arrs,seerr,plex,sab"], timeout=90)
    if r.returncode != 0:
        raise RuntimeError(f"collect.py exit={r.returncode}: {r.stderr.strip()[:300]}")
    now = utc_now()
    d = DATA_ROOT / "snapshots" / now.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    path = d / (now.strftime("%H") + ".json")
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(r.stdout, encoding="utf-8")
    os.replace(tmp, path)
    return path


# --- Step 3: logs ---------------------------------------------------------
def collect_logs() -> dict | None:
    """Append this hour's lines to logs/<date>/<app>.log.

    Returns logs.py's raw payload (app -> {source, lines, [error]}) so the
    caller can grade coverage, or None if the whole call failed. None and {}
    are deliberately different: None means "no evidence about anyone" and MUST
    NOT be graded, while {} would mean logs.py knows about no apps at all.
    """
    try:
        r = _run_mcp("logs.py",
                     ["--app", "all", "--since", "1h", "--tail", "2000", "--emit-json"],
                     timeout=60)
    except subprocess.TimeoutExpired:
        warn("logs.py timed out — no log coverage evidence this cycle")
        return None
    if r.returncode != 0:
        warn("logs.py exit=" + str(r.returncode) + ": " + r.stderr.strip()[:160])
        return None
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        warn("logs.py emitted unparseable JSON — no log coverage evidence")
        return None
    if not isinstance(payload, dict):
        warn("logs.py payload was " + type(payload).__name__ + ", expected dict")
        return None
    today = utc_now().strftime("%Y-%m-%d")
    logs_dir = DATA_ROOT / "logs" / today
    logs_dir.mkdir(parents=True, exist_ok=True)
    for app_name, entry in payload.items():
        lines = (entry or {}).get("lines")
        if not lines:
            # Still no file for a silent app -- zero-byte logs would only move
            # the noise. The disappearance is graded by the coverage ledger
            # below instead of being inferred from the directory listing.
            continue
        with open(logs_dir / (app_name + ".log"), "a", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(json.dumps(ln) + "\n")
    return payload


# --- Step 3b: log-coverage ledger -----------------------------------------
def _log_roster_ignore() -> frozenset:
    """Apps the operator has explicitly retired (comma-separated env var).
    Named apps are dropped from the ledger entirely, so a decommission stops
    paging the cycle the drop-in lands rather than needing a state edit."""
    raw = os.environ.get("QFLIX_COLLECT_LOG_ROSTER_IGNORE", "")
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def _hours_since(stamp: str | None, now: datetime) -> float:
    """Hours between an ISO stamp and `now`. Returns 0.0 on a missing or
    unparseable stamp -- 0 hours of silence can never trip a dark call, so a
    corrupt ledger entry degrades to "not dark yet" instead of paging."""
    if not stamp:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except Exception:
        return 0.0
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def classify_log_coverage(ledger: dict, payload: dict | None, now: datetime,
                          ignore=frozenset()) -> tuple[dict, dict]:
    """Grade this cycle's log payload against the ledger of who has ever
    produced lines. Pure + deterministic (no I/O, `now` injected) so the
    policy is unit-testable the way classify_qbit_stall is.

    Returns (new_ledger, report). report keys:
      roster_drop  [app]        -- ledger app absent from the payload (pages)
      source_error ["app:err"]  -- payload entry carries an error (pages)
      dark         ["app:Nh>Th"] -- silent past its own tolerance (reported)
      live/quiet   int          -- apps with / without lines this cycle
      skipped      str          -- set when the payload carried no evidence
    """
    # A ledger that survived a bad write / hand-edit must degrade to "empty",
    # not raise: update_log_coverage's except would then report the SAME
    # error every hour forever without ever rewriting the file that caused it.
    if not isinstance(ledger, dict):
        ledger = {}
    raw_apps = ledger.get("apps")
    apps = {k: dict(v) for k, v in (raw_apps or {}).items()
            if isinstance(v, dict)} if isinstance(raw_apps, dict) else {}
    report: dict = {"roster_drop": [], "source_error": [], "dark": [],
                    "live": 0, "quiet": 0}

    if payload is None:
        # logs.py failed (non-zero exit, timeout, unparseable, wrong type) --
        # collect_logs() returns None for all four. Grading here would mark
        # EVERY app a roster-drop off one transient subprocess failure: the
        # exact mass-false-page shape the SAB ghost-prune guards against. No
        # evidence means no verdict, and the ledger is left untouched.
        #
        # `is None`, NOT `not payload`: an empty dict is a DIFFERENT fact and
        # must NOT land here. logs.py builds its keys from static routing
        # tables, so {} means the tables resolved to zero apps -- the maximal
        # roster drop, the single loudest thing this ledger exists to catch.
        # Falling into the skip branch on {} would render a total collector
        # blindness as a msg fragment (`logs-ungraded`) that never reds, i.e.
        # the exact silent-vanish defect one level up. Let {} fall through:
        # `seen` stays empty, every ledger app reports roster_drop, the
        # heartbeat goes red, and the ledger is preserved so it keeps firing.
        report["skipped"] = "no-payload"
        return ledger, report

    seen = set()
    for app in sorted(payload):
        if app in ignore:
            continue
        seen.add(app)
        entry = payload.get(app)
        # logs.py emits {"source":..,"lines":[..],"error":..}; anything else
        # (null, a bare string) is graded as "present but silent" rather than
        # raising -- the app is demonstrably still in the routing table, which
        # is the only thing roster_drop claims to know.
        entry = entry if isinstance(entry, dict) else {}
        rec = apps.setdefault(app, {
            "first_seen_at": iso(now),
            "last_lines_at": None,
            "last_line_count": 0,
            "max_quiet_gap_h": 0,
            "quiet_cycles": 0,
        })
        rec["last_seen_at"] = iso(now)
        if entry.get("error"):
            report["source_error"].append(app + ":" + str(entry["error"])[:40])
        count = len(entry.get("lines") or [])
        if count:
            # Widen this app's tolerance by the gap it just CLOSED. Only a
            # closed gap counts: an app still silent has an open-ended gap
            # that would otherwise ratchet its own alarm out to infinity.
            #
            # Anchor on first_seen_at when there is no prior emission, same
            # anchor the dark check uses below. Anchoring on last_lines_at
            # alone left a first-ever emission calibrating to 0h, so a weekly
            # app (maint-window) kept the 26h floor through its whole first
            # cycle and was called dark six days out of seven.
            #
            # Bound the widening by quiet_cycles (cycles we actually observed
            # this app silent), because the timer is hourly and wall-clock
            # alone cannot tell a genuinely bursty app from a COLLECTOR
            # outage. Three days of timer downtime would otherwise hand every
            # app a 72h gap and permanently desensitise the whole ledger off
            # one incident -- and the box does lose the timer (2026-08-18 host
            # reboot). No cycle ran, so no evidence was gathered, so nothing
            # widens.
            anchor = rec.get("last_lines_at") or rec.get("first_seen_at")
            gap_h = min(int(_hours_since(anchor, now)),
                        int(rec.get("quiet_cycles") or 0))
            if gap_h > int(rec.get("max_quiet_gap_h") or 0):
                rec["max_quiet_gap_h"] = gap_h
            rec["last_lines_at"] = iso(now)
            rec["last_line_count"] = count
            rec["quiet_cycles"] = 0
            report["live"] += 1
            continue
        rec["quiet_cycles"] = int(rec.get("quiet_cycles") or 0) + 1
        report["quiet"] += 1
        # An app that has NEVER produced lines is measured from first_seen_at,
        # so a unit that was dead on arrival still surfaces rather than sitting
        # at "no baseline, no verdict" forever.
        silent_h = _hours_since(rec.get("last_lines_at") or rec.get("first_seen_at"), now)
        tolerance = max(LOG_DARK_MIN_HOURS,
                        LOG_DARK_GAP_MULT * int(rec.get("max_quiet_gap_h") or 0))
        if silent_h >= tolerance and int(rec["quiet_cycles"]) >= LOG_DARK_MIN_CYCLES:
            report["dark"].append("{}:{}h>{}h".format(app, int(silent_h), tolerance))

    for app in sorted(apps):
        if app in ignore:
            apps.pop(app, None)     # operator-retired: forget, stop grading
        elif app not in seen:
            report["roster_drop"].append(app)

    return {"apps": apps, "updated_at": iso(now)}, report


def update_log_coverage(payload: dict | None) -> dict:
    """I/O wrapper around classify_log_coverage. Never raises into main():
    a coverage-grading bug must not take down the collect cycle that the
    unstick loop and the dead-man heartbeat depend on."""
    try:
        path = DATA_ROOT / LOG_COVERAGE_FILE
        ledger: dict = {}
        if path.exists():
            try:
                ledger = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                ledger = {}
        new_ledger, report = classify_log_coverage(
            ledger, payload, utc_now(), _log_roster_ignore())
        if not report.get("skipped"):
            _write_json_atomic(path, new_ledger)
        return report
    except Exception as exc:
        warn("log-coverage grading failed (non-fatal): " + str(exc))
        return {"roster_drop": [], "source_error": [], "dark": [],
                "live": 0, "quiet": 0, "skipped": "error:" + str(exc)[:80]}


def format_log_coverage(report: dict) -> str:
    """One-line coverage fragment for the Kuma msg / journal. Empty string
    when nothing is wrong, so a healthy cycle's message is unchanged."""
    parts = []
    if report.get("roster_drop"):
        parts.append("logs-roster-drop=" + ",".join(report["roster_drop"]))
    if report.get("source_error"):
        parts.append("logs-source-error=" + ",".join(report["source_error"]))
    dark = report.get("dark") or []
    if dark:
        # _push_kuma slices msg to 200 chars. The paging classes are emitted
        # first so they always survive; dark is summarised rather than allowed
        # to push them out of the window on a day when many apps are quiet.
        # last-collect.json carries the unabridged list either way.
        head = ",".join(dark[:3])
        parts.append("logs-dark=" + head +
                     ("+{} more".format(len(dark) - 3) if len(dark) > 3 else ""))
    if report.get("skipped"):
        parts.append("logs-ungraded=" + str(report["skipped"]))
    return "; ".join(parts)[:130]


# --- Step 4: stale-state --------------------------------------------------
def _load_snapshots(last_n: int = 3) -> list[dict]:
    snap_root = DATA_ROOT / "snapshots"
    if not snap_root.is_dir():
        return []
    files = sorted(str(p) for p in snap_root.rglob("*.json"))
    out = []
    for fp in files[-last_n:]:
        try:
            out.append(json.loads(Path(fp).read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _matches_stale_sab_rule(state: str | None, queue_paused: bool,
                            has_started: bool | None = None) -> str | None:
    """Local port of the C2 `matches_stale_sab_rule` rule table (state name
    -> rule, or None if not stale-eligible). Used both by the per-snapshot
    dispatch in update_stale_state() and by escalate_sab_if_pinned()'s
    "still rule-matching" strike check. See the module-level note above on
    why this is duplicated rather than imported from scripts/mcp/collect.py.
    """
    if state == SAB_PAUSED_STATE:
        # object.py wedge: SAB force-pauses the WHOLE job when a post-restart
        # file re-import fails, with no auto-resume path. Only a wedge if the
        # queue itself isn't paused -- an operator-paused queue is normal and
        # must not be flagged.
        return "sab-paused-pinned" if not queue_paused else None
    if state in SAB_DOWNLOADISH_STATES:
        # TWO EXEMPTIONS, both added 2026-08-07 after this rule deleted AND
        # BLOCKLISTED 10 legitimate releases in a single run (see the twin in
        # scripts/mcp/collect.py for the full write-up; these two copies are
        # deliberately duplicated and MUST stay in lockstep).
        #
        # 1. A paused QUEUE leaves its slots reporting "Downloading", not
        #    "Paused", so the exemption above never fired for it.
        # 2. SAB transfers one nzb at a time while labelling every queued slot
        #    "Downloading" (1 of 146 slots held any bytes, measured live), so
        #    zero byte-movement is normal for everything behind the head and
        #    this flagged queue_depth-1 items forever.
        #
        # has_started=None = caller cannot tell; keep prior behaviour.
        # "Nothing starting at all" is queue-level and belongs to
        # canaries/sab-stall.sh, not here.
        if queue_paused:
            return None
        if has_started is False:
            return None
        return "sab-zero-movement"
    if state in SAB_PP_STATES:
        # Hung post-processing (par2/unrar/move) -- unstick's *arr-side
        # DELETE can't touch this at all; the caller sets
        # candidate_for_unstick=False and routes it to C4 escalation instead.
        return "sab-pp-hung"
    return None


# --- qBit state classification (exhaustive) --------------------------------
# We only reach the classifier for a torrent that made ZERO downloaded-delta
# across the last 3 hourly snapshots, so the question is narrow: is this
# zero-movement a genuine STALL to unstick, or an EXPECTED idle (seeding done,
# a transient disk op, legitimately queued, or a metaDL handled by its own
# rule below)?
#
# EXHAUSTIVE BY DESIGN (2026-07-27 audit). The previous whitelist silently
# `continue`d on every unlisted state, so forcedDL (Happy Face S01E08 — stuck at
# 65% / 0 seeds for ~10 weeks), error, and missingFiles were NEVER flagged for
# unstick. Any state absent from BOTH sets is UNKNOWN: the caller logs it loudly
# and skips, so a new/renamed libtorrent state surfaces in the journal instead
# of vanishing. Keep these two sets covering the full qBit `state` enum.

# Incomplete + zero-movement in one of these = a real download stall -> unstick.
_QBIT_STALL_STATES = frozenset({
    "stalledDL",              # announced, no seeds serving
    "forcedDL",               # force-started but still not moving — force
                              # bypasses the queue/ratio caps, not the lack of
                              # seeds; a forcedDL at 0 B/s for 3h IS stuck
    "pausedDL", "stoppedDL",  # paused/stopped mid-download (qBit 4.x vs 5.x name)
})
# Broken regardless of progress% -> unstick (a COMPLETE torrent can still error
# or lose its files; the pre-2026-07-27 progress>=1 early-skip hid these).
_QBIT_ERROR_STATES = frozenset({"error", "missingFiles"})
# Zero-movement here is EXPECTED — never a stall the unstick path should touch:
#   *UP / uploading / queuedUP : complete + seeding (genuinely-orphaned seeding
#                                leftovers are the torrent-janitor's job, not
#                                unstick's — unstick would blocklist a good grab)
#   checking* / allocating / moving : transient disk ops mid-flight
#   queuedDL                   : legitimately waiting behind active downloads
#   metaDL                     : handled by the dedicated meta-stuck rule
#                                (size 0 + added >=24h) later in this function
_QBIT_IDLE_STATES = frozenset({
    "uploading", "stalledUP", "forcedUP", "queuedUP", "pausedUP", "stoppedUP",
    "checkingUP", "checkingDL", "checkingResumeData", "allocating", "moving",
    "queuedDL", "metaDL",
})

# Sentinel rule returned for an unrecognized state (caller warns + skips).
_QBIT_UNKNOWN_RULE = "__unknown-state__"


def classify_qbit_stall(state, progress, dlspeed):
    """Classify a zero-movement qBit torrent by its `state`.

    Returns None to SKIP (idle / transient / complete-seeding / handled by the
    metaDL rule), or a (rule:str, candidate_for_unstick:bool) pair. An
    unrecognized state returns (_QBIT_UNKNOWN_RULE, False) so the caller can log
    it loudly and skip. Pure + deterministic (no I/O) for unit tests — the
    unknown-state warning is emitted by the caller, not here."""
    if state in _QBIT_ERROR_STATES:
        return (state, True)                 # rule == "error" | "missingFiles"
    # Past here it's a download-progress question; a complete torrent is not a
    # download stall (its seeding leftovers are the janitor's concern).
    if (progress or 0) >= 1.0:
        return None
    if state in _QBIT_STALL_STATES:
        return (state, True)
    if state == "downloading":
        # Actively "downloading" per qBit but zero net delta across 3 snapshots:
        # dead-slow if it's crawling under the floor, else leave it (rare — qBit
        # reports throughput yet the snapshots caught matching byte counts).
        if (dlspeed or 0) < DEAD_SLOW_BYTES:
            return ("dead-slow", True)
        return None
    if state in _QBIT_IDLE_STATES:
        return None
    return (_QBIT_UNKNOWN_RULE, False)


def update_stale_state() -> list[str]:
    """Port of Update-StaleState, extended for SAB per spec C3. State is a
    plain dict persisted to stale-state.json. Returns keys (qBit hashes OR
    SAB nzo_ids) that are fresh unstick candidates."""
    state_file = DATA_ROOT / "stale-state.json"
    hashes: dict = {}
    if state_file.exists():
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            hashes = dict(loaded.get("hashes", {}))
        except Exception:
            hashes = {}
    # Every tracked entry must carry a kind. A loaded legacy entry (written
    # before SAB support existed) has none -- it was always a qBit hash.
    for entry in hashes.values():
        entry.setdefault("kind", "qbit")

    snaps = _load_snapshots(3)
    if len(snaps) < 3:
        _write_json_atomic(state_file, {"hashes": hashes, "updated_at": iso()})
        return []

    # key -> [samples] across the 3 snapshots. qBit keyed by hash (40-char
    # hex), SAB keyed by nzo_id ("SABnzbd_nzo_..." strings) -- disjoint
    # namespaces that safely share one dict.
    samples: dict[str, list[dict]] = {}
    for s in snaps:
        for t in (s.get("qbit", {}) or {}).get("torrents", []) or []:
            samples.setdefault(t.get("hash"), []).append({
                "downloaded": t.get("downloaded_bytes"),
                "state": t.get("state"),
                "progress": t.get("progress"),
                "dlspeed": t.get("dl_speed_bytes_s"),
                "kind": "qbit",
            })
        for sl in (s.get("sab", {}) or {}).get("slots", []) or []:
            samples.setdefault(sl.get("id"), []).append({
                "downloaded": sl.get("downloaded_bytes"),
                "state": sl.get("state"),
                "progress": sl.get("progress"),
                "dlspeed": sl.get("dl_speed_bytes_s"),
                "kind": "sab",
            })

    latest_snap = snaps[-1]
    sab_queue_paused = bool(
        ((latest_snap.get("sab") or {}).get("queue") or {}).get("paused"))

    candidates: list[str] = []
    for k, sm in list(samples.items()):
        if len(sm) < 3:
            continue
        try:
            delta = (sm[-1]["downloaded"] or 0) - (sm[0]["downloaded"] or 0)
        except TypeError:
            continue
        if delta != 0:
            hashes.pop(k, None)   # made progress — no longer stale
            continue
        latest = sm[-1]
        kind = latest.get("kind", "qbit")
        state = latest.get("state")

        if kind == "qbit":
            verdict = classify_qbit_stall(
                state, latest.get("progress"), latest.get("dlspeed"))
            if verdict is None:
                continue
            rule, candidate_for_unstick = verdict
            if rule == _QBIT_UNKNOWN_RULE:
                # A qBit state in NEITHER the stall nor idle set. Don't act (we
                # don't know it's safe), but surface it loudly so a new/renamed
                # libtorrent state gets classified instead of being silently
                # dropped the way forcedDL was before 2026-07-27.
                warn("unrecognized qBit state " + repr(state) + " for "
                     + str(k)[:16] + " (progress=" + str(latest.get("progress"))
                     + ") — not acting; add it to classify_qbit_stall's sets")
                continue
        else:  # kind == "sab" -- same 3-snapshot zero-delta requirement (C3)
            # has_started distinguishes "stalled" from "queued behind others".
            # Zero delta is necessary but NOT sufficient for SAB: a slot that
            # has never received a byte is waiting its turn, because SAB
            # transfers one nzb at a time while reporting every queued slot as
            # "Downloading". Read the NEWEST sample, not the oldest -- a slot
            # that started during the window has genuinely started.
            has_started = any((s.get("downloaded") or 0) > 0 for s in sm)
            rule = _matches_stale_sab_rule(state, sab_queue_paused, has_started)
            if rule is None:
                # FORGET, don't just skip. `continue` alone leaves a previously
                # tracked entry banked forever with its accrued
                # consecutive_zero_hours, because nothing else prunes an entry
                # that merely stopped matching -- the qBit path pops on progress
                # (see above), the ghost-prune only fires when the id leaves the
                # client entirely, and build_stuck_list reads this state
                # verbatim. That is why 133 phantom rows survived the
                # 2026-08-07 rule fix and would have kept the heartbeat
                # reporting them stuck indefinitely.
                #
                # Convergent by construction: no longer eligible means no longer
                # tracked. A queue paused for post-processing therefore resets
                # the clock, which is correct -- while the queue is paused we
                # cannot judge, and a genuinely stalled item re-accrues as soon
                # as it resumes. Detection is delayed by the threshold, not lost.
                hashes.pop(k, None)
                continue
            # unstick's *arr-side DELETE flow can't fix a hung par2/unrar
            # step; these are tracked (feed the stuck list + C4 escalation)
            # but never handed to the per-hour unstick dispatch.
            candidate_for_unstick = rule != "sab-pp-hung"

        if k not in hashes:
            hashes[k] = {
                "first_zero_movement_at": iso(),
                "consecutive_zero_hours": ZERO_MOVEMENT_HOURS,
                "last_progress": latest.get("progress"),
                "rule_matched": rule,
                "candidate_for_unstick": candidate_for_unstick,
                "acted_on_at": None,
                "kind": kind,
            }
        else:
            prev = int(hashes[k].get("consecutive_zero_hours") or 0)
            if prev < ZERO_MOVEMENT_HOURS:
                prev = ZERO_MOVEMENT_HOURS
            hashes[k]["consecutive_zero_hours"] = prev + 1
            hashes[k]["rule_matched"] = rule
            hashes[k]["candidate_for_unstick"] = candidate_for_unstick
            hashes[k]["last_progress"] = latest.get("progress")
            hashes[k]["kind"] = kind

        # PP-state-stability tracking (council 2026-07-20, Defect 2): a
        # legitimate multi-hour par2/unrar/extract on a huge release shows the
        # SAME zero-downloaded-delta signature as a wedged PP step, so
        # consecutive_zero_hours alone can't tell them apart — escalating on it
        # would interrupt healthy post-processing. But a HEALTHY job advances
        # through PP states (Verifying -> Repairing -> Extracting -> Moving),
        # while a WEDGED one sits in one state. Track hours the SAB PP state has
        # been UNCHANGED; a transition resets it. Strike (b) fires on this, not
        # on raw zero-movement hours.
        if kind == "sab" and rule == "sab-pp-hung":
            if hashes[k].get("pp_state") == state:
                hashes[k]["pp_same_state_hours"] = int(
                    hashes[k].get("pp_same_state_hours") or 0) + 1
            else:
                hashes[k]["pp_state"] = state
                hashes[k]["pp_same_state_hours"] = 0
        elif "pp_state" in hashes[k]:
            # No longer a pp-hung entry — clear the stability tracker.
            hashes[k].pop("pp_state", None)
            hashes[k].pop("pp_same_state_hours", None)

        if candidate_for_unstick and not hashes[k].get("acted_on_at"):
            candidates.append(k)

    latest_torrents = (latest_snap.get("qbit", {}) or {}).get("torrents", []) or []

    # Ghost prune: a tracked key no longer live is resolved — unstick (or the
    # C4 escalation) removed it, or it completed/was cleaned up out-of-band.
    # Without this, acted-on entries linger in stale-state.json forever and
    # app_status.py keeps surfacing them as stuck (2026-07-19: heartbeat
    # showed 5 phantom stuck vs 0 real).
    #
    # Two independent guards (2026-07-19 SAB parity extension):
    #  1. If EITHER section errored this cycle, skip pruning ENTIRELY. A
    #     naive per-kind live-set built off an errored section's empty
    #     torrents/slots list would look like "nothing of this kind is
    #     live" and wrongly mass-prune that kind's legitimate, still-
    #     tracked entries.
    #  2. A `sab` key entirely ABSENT from the snapshot (legacy pre-SAB
    #     snapshot, or a collect run with --include lacking sab) is NOT an
    #     error -- it's just no evidence either way. qBit-kind entries still
    #     prune normally in that case, but SAB-kind entries are left alone:
    #     there is no SAB live-set to judge them against this cycle.
    qbit_section = latest_snap.get("qbit") or {}
    sab_section = latest_snap.get("sab")   # None => key absent entirely
    qbit_errored = bool(qbit_section.get("error"))
    sab_errored = bool((sab_section or {}).get("error")) if sab_section is not None else False

    if not (qbit_errored or sab_errored):
        qbit_live = {t.get("hash") for t in latest_torrents}
        sab_live = (
            {sl.get("id") for sl in sab_section.get("slots", []) or []}
            if sab_section is not None else None    # None = no SAB data this cycle
        )
        for k in list(hashes.keys()):
            entry_kind = hashes[k].get("kind", "qbit")
            if entry_kind == "qbit":
                if k not in qbit_live:
                    del hashes[k]
            elif entry_kind == "sab":
                if sab_live is None:
                    continue   # sab section missing entirely -- can't judge, keep
                if k not in sab_live:
                    del hashes[k]

    # Rule 3 (bad grab): completed torrent flagged bad — act now, no 3h wait.
    for t in latest_torrents:
        bg = t.get("bad_grab_signals") or {}
        if not bg.get("any"):
            continue
        h = t.get("hash")
        if h in hashes and hashes[h].get("acted_on_at"):
            continue
        if h not in hashes:
            rule = "bad-grab-size" if bg.get("suspicious_size") else "bad-grab-cf"
            hashes[h] = {
                "first_zero_movement_at": iso(),
                "consecutive_zero_hours": 0,
                "last_progress": t.get("progress"),
                "rule_matched": rule,
                "candidate_for_unstick": True,
                "acted_on_at": None,
                "kind": "qbit",
            }
            candidates.append(h)

    # Rule 5 (meta-stuck): metaDL + size 0 + added >=24h ago.
    now_epoch = int(utc_now().timestamp())
    for t in latest_torrents:
        if t.get("state") != "metaDL":
            continue
        if t.get("size_bytes") != 0:   # PS: metaDL whose metadata never resolved
            continue
        added = t.get("added_on")
        if not added:
            continue
        age = now_epoch - int(added)
        if age < META_STUCK_AGE_S:
            continue
        h = t.get("hash")
        if h in hashes and hashes[h].get("acted_on_at"):
            continue
        if h in hashes:
            continue
        hashes[h] = {
            "first_zero_movement_at": iso(),
            "consecutive_zero_hours": int(age / 3600),
            "last_progress": t.get("progress"),
            "rule_matched": "meta-stuck",
            "candidate_for_unstick": True,
            "acted_on_at": None,
            "kind": "qbit",
        }
        candidates.append(h)

    _write_json_atomic(state_file, {"hashes": hashes, "updated_at": iso()})
    return candidates


# --- Step 5: act ----------------------------------------------------------
# "sab-orphan-removed" is the usenet twin of "qbit-orphan-removed" (C5, SAB
# stuck-parity spec 2026-07-19) -- mirrors unstick.py's _EFFECTIVE_STATUSES.
_EFFECTIVE_RESULTS = ("deleted+blocklisted", "qbit-orphan-removed", "sab-orphan-removed")
_TERMINAL_STATUSES = ("deleted+blocklisted", "qbit-orphan-removed", "sab-orphan-removed", "already-fully-removed")


def count_todays_actions() -> int:
    """Only EFFECTIVE actions consume a daily slot; refusals stay in the
    audit log but must not gate the next attempt (same fix as unstick.py)."""
    today = utc_now().strftime("%Y-%m-%d")
    f = DATA_ROOT / "events" / (today + ".jsonl")
    if not f.exists():
        return 0
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("result") in _EFFECTIVE_RESULTS:
            n += 1
    return n


def stamp_acted_on(h: str) -> None:
    state_file = DATA_ROOT / "stale-state.json"
    if not state_file.exists():
        return
    try:
        loaded = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return
    if h not in loaded.get("hashes", {}):
        return
    loaded["hashes"][h]["acted_on_at"] = iso()
    _write_json_atomic(state_file, loaded)


def act_on_candidates(candidates: list[str]) -> list[str]:
    acted: list[str] = []
    count = count_todays_actions()
    events_dir = DATA_ROOT / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    for h in candidates:
        if count >= MAX_ACTIONS_PER_DAY:
            break
        try:
            r = _run_mcp("unstick.py",
                         ["--emit-json", "--hash", h, "--reason", "3h-zero-movement"],
                         timeout=60)
        except subprocess.TimeoutExpired:
            warn("unstick timeout for " + h)
            continue
        if r.returncode != 0:
            warn("unstick failed for " + h + ": " + r.stderr.strip()[:160])
            continue
        try:
            result = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        today = utc_now().strftime("%Y-%m-%d")
        line = {
            "ts": iso(), "action": "unstick", "hash": h,
            "result": result.get("status"), "via": "qflix-collect.py",
        }
        with open(events_dir / (today + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")
        if result.get("status") in _TERMINAL_STATUSES:
            stamp_acted_on(h)
        acted.append(h)
        count += 1
    return acted


# --- Step 5b: SAB escalation circuit-breaker (C4) --------------------------
# unstick.py's *arr-side DELETE flow is the ecosystem-standard remedy, but
# research (GH #802/#1104/#3106, reproduced live 2026-07-19) shows SAB can
# no-op it against a wedged queue object -- and a hung par2/unrar step isn't
# reachable by DELETE at all. `mode=restart_repair` (SAB restart + queue
# rebuild from disk) is the only documented remedy for either wedge class.
# It's a bigger hammer than unstick, so it's rate-limited hard: a persistent
# on-disk latch, max one fire per SAB_REPAIR_COOLDOWN_H.

def _sab_repair_latch_path() -> Path:
    return DATA_ROOT / "sab-repair-latch.epoch"


def _sab_repair_cooldown_active() -> bool:
    """True if a restart_repair fired within the cooldown window. Fails
    OPEN (cooldown NOT active) on a missing/corrupt latch file -- the worst
    case there is one extra fire, not a permanently-stuck queue."""
    p = _sab_repair_latch_path()
    if not p.exists():
        return False
    try:
        last = float(p.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    return (utc_now().timestamp() - last) < (SAB_REPAIR_COOLDOWN_H * 3600)


def _stamp_sab_repair_latch() -> None:
    p = _sab_repair_latch_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(int(utc_now().timestamp())), encoding="utf-8")


def _sab_api(mode: str) -> dict:
    """Minimal stdlib SAB API GET helper -- SELF-CONTAINED in this file.

    Deviation from the C4 spec text (which names `SabClient.restart_repair`):
    qflix-collect.py never imports scripts/mcp modules, only shells out to
    them via _run_mcp() -- that boundary is what lets this file and
    scripts/mcp/lib/sab_client.py be built concurrently by separate agents
    without either one's import graph depending on the other mid-edit.
    Mirrors _read_kuma_token's secrets-reading pattern (~/secrets/
    sabnzbd.{port,key}) and qbit_client's stdlib-urllib request shape.

    Raises FileNotFoundError if secrets are missing, or a urllib/json error
    on transport failure -- callers decide what "success" means (see
    _sab_restart_repair: a timeout/conn-reset AFTER the call was issued is
    treated as success-pending, never as failure).
    """
    secrets = Path(os.environ.get("MANITOBA_SECRETS", str(Path.home() / "secrets")))
    port = (secrets / "sabnzbd.port").read_text(encoding="utf-8").strip()
    key = (secrets / "sabnzbd.key").read_text(encoding="utf-8").strip()
    qs = urllib.parse.urlencode({"mode": mode, "apikey": key, "output": "json"})
    url = "http://127.0.0.1:" + port + "/api?" + qs
    timeout = 30 if mode == "restart_repair" else 15
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="ignore")
    return json.loads(body) if body else {}


def _sab_restart_repair() -> str:
    """Fire SAB's mode=restart_repair. SAB restarts mid-response for this
    call -- a timeout or connection-reset right after we've issued it is the
    EXPECTED happy path (per the pinned spec's research: verify by re-poll,
    never by return code), so any error surfacing after the network attempt
    is "issued-conn-drop" (success-pending), not a failure. Only a missing
    secrets file (the call was never even attempted) is a real error."""
    try:
        _sab_api("restart_repair")
        return "issued"
    except FileNotFoundError as exc:
        return "error:no-secrets:" + str(exc)[:80]
    except Exception as exc:
        return "issued-conn-drop:" + str(exc)[:80]


def escalate_sab_if_pinned() -> dict:
    """Port of C4. Called once per collect cycle, after act_on_candidates.
    Fires SAB's restart_repair circuit-breaker when either strike condition
    trips, subject to the cooldown latch. Never raises into main() -- every
    branch is defensive, and the outer try/except is the final backstop.

    Returns a diagnostic dict (mainly useful for tests); main() only cares
    that this never blows up the rest of the collect cycle.
    """
    result: dict = {"fired": False, "trigger": None, "ids": []}
    try:
        state_file = DATA_ROOT / "stale-state.json"
        if not state_file.exists():
            return result
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
        except Exception:
            return result
        hashes = loaded.get("hashes", {}) or {}

        snaps = _load_snapshots(1)
        latest_snap = snaps[-1] if snaps else {}
        sab_section = latest_snap.get("sab") or {}
        latest_slots = {sl.get("id"): sl for sl in sab_section.get("slots", []) or []}
        queue_paused = bool((sab_section.get("queue") or {}).get("paused"))

        strike_ids: list[str] = []
        trigger = None

        # Strike (a): unstick was dispatched >=1h ago, the slot is STILL
        # there, and it STILL matches a stale rule -- the *arr-side DELETE
        # no-oped against a wedged SAB queue object (mode=resume/delete
        # return {"status":true} while doing nothing -- SAB does not log
        # API calls, so re-polling is the only way to tell).
        for k, entry in hashes.items():
            if entry.get("kind") != "sab":
                continue
            acted = entry.get("acted_on_at")
            if not acted:
                continue
            slot = latest_slots.get(k)
            if slot is None:
                continue
            try:
                acted_dt = datetime.fromisoformat(acted.replace("Z", "+00:00"))
            except Exception:
                continue
            if (utc_now() - acted_dt).total_seconds() < 3600:
                continue
            if _matches_stale_sab_rule(slot.get("state"), queue_paused) is None:
                continue
            strike_ids.append(k)
            trigger = trigger or "strike-a-unstick-no-op"

        # Strike (b): a hung post-processing step (par2/unrar/move) that
        # unstick can never touch (candidate_for_unstick is False for
        # these) -- past the escalation threshold, restart_repair is the
        # only documented remedy.
        for k, entry in hashes.items():
            if entry.get("kind") != "sab" or entry.get("rule_matched") != "sab-pp-hung":
                continue
            # Fire on hours in the SAME PP state (Defect 2), not raw zero-
            # movement hours — a job still transitioning Verifying->Repairing->
            # Extracting->Moving is healthy long PP, not a wedge, and each
            # transition reset pp_same_state_hours in update_stale_state().
            if int(entry.get("pp_same_state_hours") or 0) >= PP_HUNG_ESCALATE_HOURS:
                if k not in strike_ids:
                    strike_ids.append(k)
                trigger = trigger or "strike-b-pp-hung"

        if not strike_ids:
            return result
        result["trigger"] = trigger
        result["ids"] = strike_ids

        if _sab_repair_cooldown_active():
            result["skipped"] = "cooldown"
            return result

        outcome = _sab_restart_repair()
        result["outcome"] = outcome
        # Only a call that was actually ISSUED consumes the 24h cooldown and
        # counts as a fire. `error:no-secrets` means the request never left
        # the box — stamping the latch there would burn the breaker's whole
        # daily budget on a no-op and mis-signal a fire to the event log /
        # Discord (council 2026-07-20, Defect 1). `issued` and
        # `issued-conn-drop` (SAB restarting mid-response) are both real fires.
        if outcome.startswith("error:"):
            result["skipped"] = "not-issued:" + outcome
            _notify("SAB restart_repair NOT issued (" + outcome + ") for "
                    + ", ".join(strike_ids), "error")
            return result

        _stamp_sab_repair_latch()
        result["fired"] = True

        today = utc_now().strftime("%Y-%m-%d")
        events_dir = DATA_ROOT / "events"
        events_dir.mkdir(parents=True, exist_ok=True)
        line = {"ts": iso(), "action": "sab-restart-repair", "trigger": trigger,
                "ids": strike_ids, "outcome": outcome}
        with open(events_dir / (today + ".jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n")

        _notify("SAB restart_repair fired (" + str(trigger) + "): " +
                ", ".join(strike_ids), "warning")

        # Verify by re-poll after a delay (SAB restarts mid-response; give
        # it a moment to come back before judging the outcome). Best-effort
        # observability only -- logged, never raised; the next hourly cycle
        # re-evaluates regardless of what this shows.
        try:
            time.sleep(SAB_REPAIR_VERIFY_DELAY_S)
            verify = _sab_api("queue")
            still_paused = bool((verify.get("queue") or {}).get("paused"))
            log("sab-restart-repair verify: queue.paused=" + str(still_paused))
        except Exception as exc:
            warn("sab-restart-repair verify failed (non-fatal): " + str(exc))

        return result
    except Exception as exc:
        warn("escalate_sab_if_pinned failed (non-fatal): " + str(exc))
        return result


# --- Step 7: retention ----------------------------------------------------
def _prune_dir(sub: str, days: int, files: bool = False) -> None:
    root = DATA_ROOT / sub
    if not root.is_dir():
        return
    cutoff = utc_now().timestamp() - days * 86400
    for p in root.iterdir():
        try:
            if p.stat().st_mtime >= cutoff:
                continue
            if files and p.is_file():
                p.unlink()
            elif not files and p.is_dir():
                import shutil
                shutil.rmtree(p)
        except Exception:
            continue


def prune_retention() -> None:
    _prune_dir("snapshots", 30)
    _prune_dir("logs", 7)
    _prune_dir("events", 365, files=True)
    _prune_dir("runs", 7)


# --- Main -----------------------------------------------------------------
def main() -> int:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    lock = _acquire_run_lock()
    if lock is None:
        log("prior collect still running — exiting")
        return 0
    started = utc_now()
    try:
        snap_path = collect_snapshot()
        log_payload = collect_logs()
        coverage = update_log_coverage(log_payload)
        candidates = update_stale_state()
        acted = act_on_candidates(candidates) if candidates else []
        # Runs every cycle regardless of `candidates` -- strike (b) (a hung
        # sab-pp-hung entry past threshold) never appears in `candidates`
        # (candidate_for_unstick is False for those), so gating this behind
        # `if candidates` would silently starve that whole escalation path.
        escalation = escalate_sab_if_pinned()
        if escalation.get("fired"):
            log("SAB restart_repair fired: trigger=" + str(escalation.get("trigger")) +
                " ids=" + ",".join(escalation.get("ids") or []))
        prune_retention()

        try:
            snap = json.loads(Path(snap_path).read_text(encoding="utf-8"))
            tcount = len((snap.get("qbit", {}) or {}).get("torrents", []) or [])
        except Exception:
            tcount = -1
        dur = round((utc_now() - started).total_seconds(), 2)
        msg = (f"Snapshot {started.strftime('%H')}.json: {tcount} torrents, "
               f"{len(candidates)} stale candidates, {len(acted)} actions")

        # Coverage rides the EXISTING rails rather than a new monitor: the
        # fragment lands in the Kuma msg, the journal, and last-collect.json.
        # roster-drop / source-error are unambiguous collector-integrity
        # breaks (logs.py's app keys come from static tables), so they also
        # turn the heartbeat red and post to Discord. `dark` never reds --
        # quiet-healthy and gone are indistinguishable from here, and a red
        # nobody can clear is a red everybody mutes.
        cov_msg = format_log_coverage(coverage)
        if cov_msg:
            warn("log coverage: " + cov_msg)
            msg = msg + "; " + cov_msg
        cov_broken = bool(coverage.get("roster_drop") or coverage.get("source_error"))

        log(msg + f" ({dur}s)")
        _notify(msg, "info")
        if cov_broken:
            _notify("Collector lost log coverage: " + cov_msg, "error")
        _push_kuma("down" if cov_broken else "up", msg)

        _write_json_atomic(DATA_ROOT / "last-collect.json", {
            "ts": iso(started), "exit_code": 0, "duration_s": dur,
            "snapshot_path": str(snap_path), "torrent_count": tcount,
            "candidates": len(candidates), "actions": len(acted),
            "log_coverage": coverage,
        })
        return 0
    except Exception as exc:
        err = str(exc)
        warn("collect failed: " + err)
        _notify("Collect failed: " + err, "error")
        _push_kuma("down", "collect failed: " + err[:160])
        _write_json_atomic(DATA_ROOT / "last-collect.json", {
            "ts": iso(started), "exit_code": 1, "error": err,
        })
        return 1
    finally:
        _release_run_lock(lock)


if __name__ == "__main__":
    sys.exit(main())

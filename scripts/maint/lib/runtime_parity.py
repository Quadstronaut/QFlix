"""lib/runtime_parity.py - per-minute runtime-parity predicates (QFLX-20, spec 5.8).

WHY THIS EXISTS: after a UCC app is swapped for a native systemd unit the old
container stays installed and STOPPED as the rollback target. If anything wakes
it, two runtimes share one config dir and one port (I-6). The audit-live timer
fires only every 6h, so the detector for that lives in the per-minute pusher
path instead. Predicates (each a violation string; empty list = parity holds):

  woken-container   a PID whose /proc/<pid>/cgroup belongs to a container and
                    whose cmdline matches the app (the dormant container woke)
  process-trees     >1 process tree under our uid matches the unit's ExecStart
                    (`pgrep -u <uid> -f`, NEVER health.py's bare `pgrep -f`,
                    which is not uid-scoped and counts other tenants' processes)
  port-owner        the listener on the app's port is not the unit's MainPID
                    (or a descendant of it)

The unit-not-active predicate is health.py `require_unit_active`, because it
shares the app's own probe result.

APPLICABILITY: only converted apps (class systemd + ucc_slug) that are not in
the `pending-swap` manifest state (UCC still legitimately serves then). On the
live stack nothing is converted yet, so check() returns [] for every app.

FAIL OPEN: a predicate that cannot run (missing /proc, systemctl error, secret
unreadable) logs a warning and contributes no violation. A broken detector must
not page as a broken app or trigger a restart; the audit-live leg and the next
cycle still look.

Everything touching the machine goes through a `Host` so tests supply fakes.
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

PARITY_PREFIX = "runtime-parity:"
# cgroup path fragments that identify a container runtime. Unverified on the
# Ultra.cc slot (the QFLX-20 box proof records a real container cgroup); the
# `ucc_cgroup_markers` manifest key on the app overrides it.
DEFAULT_CGROUP_MARKERS = ("docker", "libpod", "podman", "containerd", "crio")
_RUN_TIMEOUT_S = 5.0


class Host:
    """The machine, as the predicates see it. Real implementation."""

    def __init__(self, proc_root: str | Path = "/proc"):
        self.proc_root = Path(proc_root)

    def run(self, cmd: list[str]) -> tuple[int, str]:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=_RUN_TIMEOUT_S)
        return cp.returncode, cp.stdout

    def uid(self) -> int:
        import os
        return os.getuid()

    def pids(self) -> list[int]:
        return sorted(int(p.name) for p in self.proc_root.iterdir()
                      if p.name.isdigit())

    def read(self, pid: int, name: str) -> str:
        return (self.proc_root / str(pid) / name).read_text(
            encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def applies(app) -> bool:
    """A converted app (class systemd with a ucc_slug) that is not pending-swap."""
    raw = getattr(app, "raw", None)
    if not isinstance(raw, dict):
        return False
    if getattr(app, "class_", None) != "systemd":
        return False
    if not isinstance(raw.get("ucc_slug"), str) or not raw.get("ucc_slug"):
        return False
    if raw.get("swap_state") == "pending-swap":
        return False
    return isinstance(raw.get("unit"), str) and bool(raw.get("unit"))


def parse_uid(status_text: str) -> Optional[int]:
    for line in status_text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            if len(parts) > 1 and parts[1].isdigit():
                return int(parts[1])
    return None


def parse_ppid(stat_text: str) -> Optional[int]:
    """ppid from /proc/<pid>/stat; the comm field may contain spaces/parens, so
    split after the LAST ')'."""
    _, sep, rest = stat_text.rpartition(")")
    fields = rest.split()
    if not sep or len(fields) < 2 or not fields[1].lstrip("-").isdigit():
        return None
    return int(fields[1])


def parse_show(text: str) -> dict:
    props = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            props[k.strip()] = v.strip()
    return props


def execstart_path(show_text: str) -> Optional[str]:
    """Binary path from `systemctl show -p ExecStart`:
    ExecStart={ path=/home/u/.apps/x/bin/x ; argv[]=... ; ... }"""
    m = re.search(r"path=(\S+)", show_text)
    return m.group(1) if m else None


def count_trees(pids: list[int], ppid_of) -> int:
    """Number of process trees: pids whose parent is not itself in the set."""
    s = set(pids)
    return sum(1 for p in s if ppid_of(p) not in s)


def is_descendant(pid: int, root: int, ppid_of, limit: int = 64) -> bool:
    cur: Optional[int] = pid
    for _ in range(limit):
        if cur is None or cur <= 1:
            return False
        if cur == root:
            return True
        cur = ppid_of(cur)
    return False


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

_APPS_ROOT_RE = re.compile(r"/\.apps/([^/\s]+)/?$")


def _mounts_sibling_config(host: Host, pid: int, slug: str) -> bool:
    """True when *pid*'s container bind-mounts ANOTHER app's ~/.apps/<x> at
    /config. sonarr and sonarr2 containers run the identical cmdline
    (/app/sonarr/bin/Sonarr -data=/config, s6 "svc-sonarr"), so the cmdline
    needle alone flags the live sibling as "sonarr woken" (box, 2026-10-10).
    Only a POSITIVE identification of a different slug excludes the pid: an
    unreadable mountinfo or no /config mount keeps the hit (fail toward red)."""
    try:
        text = host.read(pid, "mountinfo")
    except OSError:
        return False
    for line in text.splitlines():
        f = line.split()
        if len(f) > 4 and f[4] == "/config":
            m = _APPS_ROOT_RE.search(f[3])
            if m and m.group(1) != slug:
                return True
    return False


def woken_container(host: Host, app, uid: int) -> list[str]:
    slug = app.raw["ucc_slug"]
    unit = app.raw["unit"]
    markers = tuple(app.raw.get("ucc_cgroup_markers") or DEFAULT_CGROUP_MARKERS)
    needles = {slug.lower()}
    pat = (app.health.raw or {}).get("pattern") if getattr(app, "health", None) else None
    if isinstance(pat, str) and pat:
        needles.add(pat.lower())
    hits = []
    for pid in host.pids():
        try:
            if parse_uid(host.read(pid, "status")) != uid:
                continue
            cg = host.read(pid, "cgroup")
            if unit in cg or not any(m in cg for m in markers):
                continue
            cmd = host.read(pid, "cmdline").replace("\0", " ").lower()
        except OSError:
            continue                      # process exited mid-scan
        if any(n in cmd for n in needles) and not _mounts_sibling_config(host, pid, slug):
            hits.append(pid)
    if hits:
        return [f"dormant container woken: {slug} pid(s) {','.join(map(str, hits[:5]))}"]
    return []


def _ppid_of(host: Host):
    def f(pid: int) -> Optional[int]:
        try:
            return parse_ppid(host.read(pid, "stat"))
        except OSError:
            return None
    return f


def process_trees(host: Host, app, uid: int, show: dict, show_text: str) -> list[str]:
    path = execstart_path(show_text)
    if not path:
        return []
    rc, out = host.run(["pgrep", "-u", str(uid), "-f", "--", re.escape(path)])
    if rc != 0:                           # 1 = nothing matches; 2+ = pgrep error
        return []
    pids = [int(p) for p in out.split() if p.isdigit()]
    n = count_trees(pids, _ppid_of(host))
    if n > 1:
        return [f"{n} process trees match ExecStart {path}"]
    return []


def fwd_unit(unit: str) -> Optional[str]:
    """The companion loopback forwarder of *unit* (QFLX-28): qflix-x.service ->
    qflix-x-fwd.service. Kestrel binds ONE address, so a .NET app binds the
    docker bridge and qflix-x-fwd.socket owns the loopback listener."""
    if not unit.endswith(".service") or unit.endswith("-fwd.service"):
        return None
    return unit[: -len(".service")] + "-fwd.service"


def is_forwarder(host: Host, pid: int, unit: str) -> bool:
    """True when *pid* is the app's own socket forwarder: a process in the
    companion <stem>-fwd.service cgroup (systemd-socket-proxyd), or the user
    systemd manager (init.scope) holding the not-yet-activated .socket. ss only
    names pids of our own uid, so a stranger never reaches this check as ours."""
    fwd = fwd_unit(unit)
    if not fwd:
        return False
    try:
        cg = host.read(pid, "cgroup")
    except OSError:
        return False
    for line in cg.splitlines():
        path = line.rsplit(":", 1)[-1].strip()
        if path.endswith("/" + fwd):
            return True
        if path.endswith("/init.scope") and "/user@" in path:
            return True
    return False


def port_owner(host: Host, app, show: dict, port: int) -> list[str]:
    rc, out = host.run(["ss", "-tlnpH", f"sport = :{port}"])
    if rc != 0:
        return []
    main = show.get("MainPID", "0")
    main_pid = int(main) if main.isdigit() else 0
    owners = {int(p) for p in re.findall(r"pid=(\d+)", out)}
    listening = any(re.search(rf":{port}\s", line + " ") for line in out.splitlines())
    if not listening:
        return []                         # not listening is the probe's finding
    if main_pid <= 0:
        return [f"port {port} is listening but the unit has no MainPID"]
    if not owners:
        return [f"port {port} listener is not owned by our uid (unit MainPID {main_pid})"]
    ppid_of = _ppid_of(host)
    unit = app.raw.get("unit", "")
    stray = sorted(p for p in owners if not is_descendant(p, main_pid, ppid_of)
                   and not is_forwarder(host, p, unit))
    if stray:
        return [f"port {port} owned by pid {stray[0]}, not unit MainPID {main_pid}"]
    return []


def check(app, host: Host | None = None, port: int | None = None) -> list[str]:
    """All parity violations for *app*; [] when it does not apply or all hold."""
    try:
        if not applies(app):
            return []
        host = host or Host()
        uid = host.uid()
        unit = app.raw["unit"]
        violations: list[str] = []

        def guarded(fn, *a):
            try:
                violations.extend(fn(*a))
            except Exception as exc:                       # noqa: BLE001 - fail open
                log.warning("runtime-parity %s %s skipped: %s", app.name, fn.__name__, exc)

        guarded(woken_container, host, app, uid)
        try:
            rc, show_text = host.run(["systemctl", "--user", "show", unit,
                                      "-p", "MainPID", "-p", "ExecStart", "--no-pager"])
        except Exception as exc:                           # noqa: BLE001
            log.warning("runtime-parity %s systemctl show failed: %s", app.name, exc)
            return violations
        if rc != 0:
            return violations
        show = parse_show(show_text)
        guarded(process_trees, host, app, uid, show, show_text)
        if port is None:
            try:
                from lib import health as health_mod
                port = health_mod._resolve_port(app)
            except Exception as exc:                       # noqa: BLE001
                log.warning("runtime-parity %s port unresolved: %s", app.name, exc)
        if port is not None:
            guarded(port_owner, host, app, show, port)
        return violations
    except Exception as exc:                               # noqa: BLE001 - fail open
        log.warning("runtime-parity %s skipped: %s", getattr(app, "name", "?"), exc)
        return []

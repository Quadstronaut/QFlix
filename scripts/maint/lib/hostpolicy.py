"""lib/hostpolicy.py -- the one place that knows which kind of host this is.

Spec: docs/superpowers/specs/2026-10-09-ucc-divorce-design.md section 5.6
(QFLX-16). Everything host-specific (the maintenance window, the UCC gate
probe, the quota source, the task ceiling, the docker gateway, the port source,
the proxy reload, the upgrade sweep) hangs off a policy object chosen by the
`host.profile` secret. Callers never branch on "am I on Ultra" themselves.

THE LAWS THIS FILE OBEYS
  * FAIL CLOSED (I-12). `host.profile` is an explicit secret, either `ultra`
    or `generic`. Missing, empty or unknown raises HostProfileError (exit 2 in
    the CLI). We NEVER default to ultra because the panel tooling happens to
    exist; that is a guess, and a wrong guess on a new box would run
    Ultra-only operations against it.
  * CROSS-CHECK. The declared profile is compared with the policy's detect().
    A mismatch is also HostProfileError. The secret is the authority; detect()
    exists only to catch a copied-over secret on the wrong host.
  * LAZY. Nothing is read at import time. Tests point MANITOBA_SECRETS_DIR at
    a tmp dir with monkeypatch.setenv after import.
  * FLAT. This module and its siblings are plain files in scripts/maint/lib,
    a merged namespace package. NEVER add an __init__.py there. Siblings are
    loaded by file path so this works as `lib.hostpolicy`, as a bare import,
    and as a script (the shell canaries run it as `python3 hostpolicy.py`).

THE ONE EXCEPTION TO FAIL-CLOSED: in_maintenance_window()
  The maintenance window is a SAFETY brake ("no box operations Monday
  11:00-15:00 UTC"). A job asking "may I operate right now?" while the profile
  secret is not yet written must get the RESTRICTIVE answer, not a crash that
  takes the job down and not a permissive default. So when the profile cannot
  be resolved, in_maintenance_window() answers with the Ultra window. That is
  exactly what every caller hard-coded before this module existed, so
  deploying the code before writing the secret changes nothing.

`in_maintenance_window(now)` is the WALL-CLOCK predicate only. Callers still
OR it with their own window-lock leg (the lock is runtime state, not policy).
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

PROFILE_SECRET = "host.profile"
PROFILES = ("ultra", "generic")

EXIT_PROFILE = 2          # missing / unknown / mismatched profile


class HostProfileError(Exception):
    """The host profile cannot be trusted. The CLI maps this to exit 2."""


def _sibling(name: str):
    """Load scripts/maint/lib/<name>.py by PATH (never via the `lib` namespace,
    which an ambient PYTHONPATH could satisfy from a different copy)."""
    key = "_qflix_hp_" + name
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().parent / (name + ".py")
    spec = importlib.util.spec_from_file_location(key, str(path))
    if spec is None or spec.loader is None:
        raise HostProfileError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


def _secrets_dir() -> Path:
    return _sibling("secrets").secrets_dir()


def read_secret(name: str) -> Optional[str]:
    """Stripped secret text, or None when absent/unreadable/empty."""
    try:
        text = (_secrets_dir() / name).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return text or None


def _utc(now: Optional[_dt.datetime]) -> _dt.datetime:
    if now is None:
        return _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=_dt.timezone.utc)
    return now.astimezone(_dt.timezone.utc)


class HostPolicy:
    """Protocol (spec 5.6). Subclasses fill in the host-specific answers."""

    name = ""

    def detect(self) -> bool:
        raise NotImplementedError

    def windows(self) -> List[Tuple[int, int, int]]:
        """(weekday 0=Mon, start_hour_utc, end_hour_utc) tuples, end exclusive.
        May be empty (a host with no maintenance window)."""
        raise NotImplementedError

    def in_window(self, now: Optional[_dt.datetime]) -> bool:
        now = _utc(now)
        return any(now.weekday() == d and s <= now.hour < e
                   for d, s, e in self.windows())

    def may_operate(self, now: Optional[_dt.datetime]) -> bool:
        return not self.in_window(now)

    def gate_probe(self) -> Optional[str]:
        raise NotImplementedError

    def port_candidates(self) -> Sequence[int]:
        raise NotImplementedError

    def proxy_reload(self) -> Optional[List[str]]:
        raise NotImplementedError

    def quota(self) -> str:
        raise NotImplementedError

    def task_ceiling(self) -> Optional[int]:
        raise NotImplementedError

    def docker_gateway(self) -> Optional[str]:
        raise NotImplementedError

    def upgrade_sweeps(self) -> List[str]:
        raise NotImplementedError


def _policy_for(profile: str) -> HostPolicy:
    if profile == "ultra":
        return _sibling("hostpolicy_ultra").UltraPolicy()
    if profile == "generic":
        return _sibling("hostpolicy_generic").GenericPolicy()
    raise HostProfileError("unknown host.profile %r (want one of %s)"
                           % (profile, ", ".join(PROFILES)))


def load(check_detect: bool = True) -> HostPolicy:
    """Resolve the policy from the `host.profile` secret. Fails closed."""
    profile = read_secret(PROFILE_SECRET)
    if profile is None:
        raise HostProfileError(
            "secret %s is missing or empty in %s; refusing to guess a profile"
            % (PROFILE_SECRET, _secrets_dir()))
    policy = _policy_for(profile.lower())
    if check_detect and not policy.detect():
        raise HostProfileError(
            "host.profile=%s but this host does not look like one (detect() "
            "failed); wrong secret on this box?" % policy.name)
    return policy


def in_maintenance_window(now: Optional[_dt.datetime] = None) -> bool:
    """Wall-clock maintenance-window predicate, policy-backed (O-5).

    Unresolvable profile -> the Ultra window (restrictive; see module doc).
    """
    try:
        policy = load(check_detect=False)
    except HostProfileError:
        policy = _sibling("hostpolicy_ultra").UltraPolicy()
    return policy.in_window(_utc(now))


def _parse_now(text: Optional[str]) -> Optional[_dt.datetime]:
    if not text:
        return None
    return _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI for shell callers.

      hostpolicy.py in-window [ISO8601]   exit 0 in window, 1 not, 3 bad input
      hostpolicy.py preflight             exit 0 ok, 2 profile missing/mismatch
      hostpolicy.py task-ceiling          prints the task ceiling; exit 2 unknown
    """
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = args[0] if args else ""
    if cmd == "in-window":
        try:
            now = _parse_now(args[1] if len(args) > 1 else os.environ.get("QFLIX_NOW"))
        except ValueError:
            sys.stderr.write("hostpolicy: unparseable time\n")
            return 3
        return 0 if in_maintenance_window(now) else 1
    if cmd == "preflight":
        try:
            pol = load()
        except HostProfileError as exc:
            sys.stderr.write("hostpolicy: %s\n" % exc)
            return EXIT_PROFILE
        print(pol.name)
        return 0
    if cmd == "task-ceiling":
        # QFLX-25: the per-user task ceiling the swap's 70% gate divides by
        # (spec 5.9 step 2.4). Unknown is a refusal, never "unlimited".
        try:
            ceiling = load().task_ceiling()
        except HostProfileError as exc:
            sys.stderr.write("hostpolicy: %s\n" % exc)
            return EXIT_PROFILE
        if not isinstance(ceiling, int) or ceiling <= 0:
            sys.stderr.write("hostpolicy: task ceiling unknown\n")
            return EXIT_PROFILE
        print(ceiling)
        return 0
    sys.stderr.write("usage: hostpolicy.py in-window [ISO8601] | preflight | task-ceiling\n")
    return 3


if __name__ == "__main__":
    sys.exit(main())

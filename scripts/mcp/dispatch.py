#!/usr/bin/env python3
"""scripts/mcp/dispatch.py — the QFlix Admin forced command.

Single SSH entry point for the Android app. Reads one verb from
$SSH_ORIGINAL_COMMAND, routes it, and emits one JSON envelope on stdout.

PRIVACY (spec 2026-08-03): no verb returns Plex sessions, watch history, or
per-member data. scripts/mcp/plex.py supports a sessions snapshot; that mode is
deliberately unreachable from here. A test asserts the verb table stays clean —
adding such a verb is a spec change, not an implementation detail.

BLAST RADIUS: the operator accepted full blast radius on 2026-08-03. This file
is a MANIFEST of what the phone can do, not a security boundary. It is still the
one place the action set is written down.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                   # scripts/mcp/lib
sys.path.insert(0, str(HERE.parent / "maint"))  # scripts/maint/lib

MAX_LINES = 20
MAX_LINES_CEILING = 200


@dataclass
class VerbSpec:
    handler: Callable
    arity: int          # required positional args after the verb
    help: str


def envelope(*, verb: str, target: Optional[str], ok: bool, verdict: str,
             lines: List[str], elapsed_s: float, max_lines: int = MAX_LINES) -> dict:
    """The one shape every verb returns, success or failure.

    `verdict` is a single human sentence — it is what the phone toasts.
    `lines` is the expandable detail, capped so a flaky mobile link is never
    asked to carry an unbounded log.
    """
    capped = min(max(int(max_lines), 1), MAX_LINES_CEILING)
    return {
        "ok": bool(ok),
        "verb": verb,
        "target": target,
        "verdict": verdict,
        "lines": [str(x) for x in (lines or [])][-capped:],
        "elapsed_s": round(float(elapsed_s), 2),
    }


VERBS: dict = {}


def _help_lines() -> List[str]:
    return ["%-24s %s" % (name, spec.help) for name, spec in sorted(VERBS.items())]


def _verb_help(argv: List[str]) -> dict:
    return envelope(verb="help", target=None, ok=True,
                    verdict="%d verbs available" % len(VERBS),
                    lines=_help_lines(), elapsed_s=0.0,
                    max_lines=MAX_LINES_CEILING)


# Registered HERE, not in Task 2: it makes the privacy test above non-vacuous
# from the first commit, and `help` needs nothing from the router.
VERBS["help"] = VerbSpec(handler=_verb_help, arity=0, help="list every verb")


def parse_command(raw: Optional[str]) -> tuple:
    """Split $SSH_ORIGINAL_COMMAND into (verb, args).

    An empty command means the operator SSH'd with no command at all — answer
    with help rather than an opaque failure.
    """
    if not raw or not raw.strip():
        return ("help", [])
    parts = raw.strip().split()
    return (parts[0], parts[1:])


def dispatch(argv: List[str]) -> dict:
    started = time.time()
    verb = argv[0] if argv else "help"
    args = argv[1:]

    spec = VERBS.get(verb)
    if spec is None:
        return envelope(verb=verb, target=None, ok=False,
                        verdict="unknown verb %r" % verb,
                        lines=_help_lines(), elapsed_s=time.time() - started,
                        max_lines=MAX_LINES_CEILING)

    if len(args) < spec.arity:
        return envelope(verb=verb, target=None, ok=False,
                        verdict="%s expects %d argument(s), got %d"
                                % (verb, spec.arity, len(args)),
                        lines=[spec.help], elapsed_s=time.time() - started)

    try:
        return spec.handler(args)
    except Exception as exc:  # a handler must never take the connection down
        return envelope(verb=verb, target=(args[0] if args else None), ok=False,
                        verdict="%s failed: %s" % (verb, exc.__class__.__name__),
                        lines=[str(exc)], elapsed_s=time.time() - started)


LIFECYCLE_CLASSES = ("ucc", "systemd")
_MANIFEST_PATH = HERE.parent.parent / "manifest" / "apps.yaml"


def _load_manifest():
    from lib import manifest as manifest_mod
    return manifest_mod.load(_MANIFEST_PATH)


def _verb_app_list(argv: List[str]) -> dict:
    started = time.time()
    man = _load_manifest()
    rows = []
    # DEVIATION from brief: `man.apps()` returns an Iterator[App] (full
    # objects), not name strings — `sorted(man.apps())` raises TypeError
    # (App is an undecorated @dataclass, not orderable) and `man.app(name)`
    # expects a string key, not an App. Sort the App objects by .name and
    # read .name/.class_ straight off them; see task-3-report.md.
    for app in sorted(man.apps(), key=lambda a: a.name):
        if app.class_ in LIFECYCLE_CLASSES:
            rows.append("%s %s" % (app.name, app.class_))
    return envelope(verb="app.list", target=None, ok=True,
                    verdict="%d apps with a lifecycle" % len(rows),
                    lines=rows, elapsed_s=time.time() - started,
                    max_lines=MAX_LINES_CEILING)


# app_status.py emits five sections. `top5` is per-member BY NAME —
# top5_watch gives {"user": friendly_name, "hours", "plays"} from Tautulli and
# top5_requests gives {"user": displayName, "count"} from Seerr. That is
# exactly the member viewing activity the spec forbids, so `status` asks for
# every section EXCEPT top5. `streams` is kept: it is aggregate counts
# (streams/users/transcodes/wan_kbps), not identities.
STATUS_SECTIONS = "quota,kuma,streams,downloads"


def _verb_status(argv: List[str]) -> dict:
    """The Dashboard doc, minus the per-member section.

    NOT a bare passthrough. Heartbeat v2's forced command ran app_status.py
    with no arguments and therefore received top5; this verb is the first
    caller that filters, which is what makes the privacy constraint true on
    the wire rather than only in the UI.
    """
    import subprocess
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(HERE / "app_status.py"), "--emit-json",
         "--sections", STATUS_SECTIONS],
        capture_output=True, text=True, timeout=30)
    ok = proc.returncode == 0 and bool(proc.stdout.strip())
    env = envelope(verb="status", target=None, ok=ok,
                   verdict="status doc emitted" if ok else "app_status.py failed",
                   lines=(proc.stderr or "").splitlines(),
                   elapsed_s=time.time() - started)
    if ok:
        env["doc"] = json.loads(proc.stdout)
    return env


VERBS["app.list"] = VerbSpec(handler=_verb_app_list, arity=0,
                             help="list the apps that have a lifecycle, with their class")
VERBS["status"] = VerbSpec(handler=_verb_status, arity=0,
                           help="the Dashboard status document")


def main() -> int:
    verb, args = parse_command(os.environ.get("SSH_ORIGINAL_COMMAND"))
    # argv beats the env var so the script is testable and hand-runnable.
    if len(sys.argv) > 1:
        verb, args = sys.argv[1], sys.argv[2:]
    json.dump(dispatch([verb] + args), sys.stdout)
    sys.stdout.write("\n")
    return 0   # see Global Constraints: the body carries failure, not the code


if __name__ == "__main__":
    raise SystemExit(main())

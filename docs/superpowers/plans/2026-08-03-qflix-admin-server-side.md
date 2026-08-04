# QFlix Admin — Server Side Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the SSH dispatcher and two library-reporting scripts that let the QFlix Admin phone app read status and fire remediation actions on the seedbox.

**Architecture:** One new forced command, `scripts/mcp/dispatch.py`, reads a verb from `$SSH_ORIGINAL_COMMAND`, routes it to an existing MCP script or to `lib.lifecycle`, and returns a single JSON envelope. Every phone action is one verb. Two new reporting scripts (`arr_library_peek.py`, `arr_disk_usage.py`) supply the stARR page.

**Tech Stack:** Python 3.9 (the box's `/usr/bin/python3`), stdlib only in the dispatcher, pytest for tests. Reuses `scripts/maint/lib/{manifest,lifecycle,suppression}.py` and `scripts/mcp/{app_status,missing,unstick,logs}.py`.

**Spec:** `docs/superpowers/specs/2026-08-03-qflix-admin-android-design.md`

**This is plan 1 of 2.** Plan 2 covers the Android app (rename to QFlix Admin, navigation drawer, three pages). The app is useless without this; this is useful on its own over plain SSH.

## Global Constraints

- **Python 3.9 on the box.** No `match` statements, no `X | Y` unions at runtime. `from __future__ import annotations` is present in every existing MCP script — keep it.
- **Import self-location, exactly as existing MCP scripts do it:**
  ```python
  HERE = Path(__file__).resolve().parent
  sys.path.insert(0, str(HERE))                   # scripts/mcp/lib
  sys.path.insert(0, str(HERE.parent / "maint"))  # scripts/maint/lib
  ```
- **`--emit-json` always exits 0.** The JSON body carries failure detail. Returning non-zero makes the caller discard the body as an SSH failure and masks which thing broke. This is a hard-won convention — see the comment in `scripts/mcp/missing.py:main`.
- **PRIVACY: no verb may return Plex sessions, watch history, or per-member data.** Enforced by a test, not a comment. `scripts/mcp/plex.py` supports a sessions mode; it stays unreachable.
- **No secrets in the repo.** Keys and host config live on the box / on-device. `tests/unit/test_no_pii_in_repo.py` enforces it.
- **Tests live at `tests/unit/test_mcp_<name>.py`**, matching the existing `test_mcp_app_status.py`, `test_mcp_missing.py`, `test_mcp_unstick.py`.
- **`MANITOBA_DRY_RUN=1`** makes `lib.lifecycle` skip subprocess calls. Use it in tests rather than mocking subprocess.

## File Structure

| File | Responsibility |
|---|---|
| `scripts/mcp/dispatch.py` (new) | Verb registry, envelope, `SSH_ORIGINAL_COMMAND` parsing, routing |
| `scripts/mcp/arr_library_peek.py` (new) | Coarse per-title presence/counts for one *arr |
| `scripts/mcp/arr_disk_usage.py` (new) | Bytes on disk managed by one *arr |
| `tests/unit/test_mcp_dispatch.py` (new) | Registry, envelope, routing, privacy guard |
| `tests/unit/test_mcp_arr_library_peek.py` (new) | Peek shaping |
| `tests/unit/test_mcp_arr_disk_usage.py` (new) | Usage shaping |
| `scripts/configure/provision-admin-key.sh` (new) | Mint key, patch `authorized_keys`, print the bundle |

---

### Task 1: Verb registry, envelope, and the privacy guard

**Files:**
- Create: `scripts/mcp/dispatch.py`
- Test: `tests/unit/test_mcp_dispatch.py`

**Interfaces:**
- Produces: `VERBS: dict[str, VerbSpec]`, `VerbSpec(handler, arity, help)`, `envelope(verb, target, ok, verdict, lines, elapsed_s) -> dict`, `MAX_LINES = 20`, `MAX_LINES_CEILING = 200`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_mcp_dispatch.py
import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def _load():
    path = REPO / "scripts" / "mcp" / "dispatch.py"
    spec = importlib.util.spec_from_file_location("dispatch", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dispatch"] = mod
    spec.loader.exec_module(mod)
    return mod

def test_envelope_has_the_documented_keys():
    d = _load()
    env = d.envelope(verb="status", target=None, ok=True,
                     verdict="all good", lines=["a", "b"], elapsed_s=1.5)
    assert set(env) == {"ok", "verb", "target", "verdict", "lines", "elapsed_s"}
    assert env["ok"] is True
    assert env["verdict"] == "all good"

def test_envelope_caps_lines_so_a_phone_never_pulls_an_unbounded_log():
    d = _load()
    env = d.envelope(verb="logs", target="sonarr", ok=True, verdict="ok",
                     lines=[str(i) for i in range(500)], elapsed_s=0.1)
    assert len(env["lines"]) == d.MAX_LINES

def test_no_verb_returns_member_viewing_activity():
    """The privacy constraint is structural: the capability must be ABSENT
    from the wire protocol, not merely unused by the UI. plex.py supports a
    sessions snapshot; no verb may reach it."""
    d = _load()
    # Non-vacuity guard: an empty VERBS would make the loop below assert
    # nothing and pass forever. `help` is registered from Task 1 precisely so
    # this test has something to check from the first commit.
    assert len(d.VERBS) >= 1, "VERBS is empty - the loop below would prove nothing"
    banned = ("session", "watch", "history", "viewer", "member", "who")
    for name in d.VERBS:
        low = name.lower()
        assert not any(b in low for b in banned), (
            "verb %r looks like it exposes member activity; see the privacy "
            "constraint in the 2026-08-03 spec" % name)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: FAIL — `dispatch.py` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/dispatch.py tests/unit/test_mcp_dispatch.py
git commit -m "feat(dispatch): envelope + verb registry, with the privacy guard as a test"
```

---

### Task 2: `SSH_ORIGINAL_COMMAND` parsing and routing

**Files:**
- Modify: `scripts/mcp/dispatch.py`
- Test: `tests/unit/test_mcp_dispatch.py`

**Interfaces:**
- Consumes: `VERBS`, `envelope` from Task 1
- Produces: `parse_command(raw: Optional[str]) -> tuple[str, list]`, `dispatch(argv: list) -> dict`, `main() -> int`

- [ ] **Step 1: Write the failing test**

```python
def test_parse_command_splits_verb_from_args():
    d = _load()
    assert d.parse_command("app.restart sonarr") == ("app.restart", ["sonarr"])

def test_empty_command_is_help_not_a_crash():
    d = _load()
    assert d.parse_command(None) == ("help", [])
    assert d.parse_command("   ") == ("help", [])

def test_unknown_verb_lists_every_known_verb_so_the_app_cannot_silently_noop():
    d = _load()
    env = d.dispatch(["definitely.not.a.verb"])
    assert env["ok"] is False
    assert "unknown verb" in env["verdict"].lower()
    for name in d.VERBS:
        assert any(name in line for line in env["lines"])

def test_wrong_arity_says_what_was_expected():
    """Registers a throwaway probe verb rather than naming a real one.

    At Task 2 the only registered verb is `help` (arity 0); every verb that
    takes an argument arrives in Task 4 or later. Naming one of those here
    would make this test pass for the wrong reason now (it would hit the
    unknown-verb branch) and couple it to another task's registration order
    forever."""
    d = _load()
    d.VERBS["probe.needs_one"] = d.VerbSpec(
        handler=lambda argv: d.envelope(verb="probe.needs_one", target=argv[0],
                                        ok=True, verdict="ok", lines=[],
                                        elapsed_s=0.0),
        arity=1, help="arity probe")
    env = d.dispatch(["probe.needs_one"])      # missing the one required arg
    assert env["ok"] is False
    assert "expects" in env["verdict"].lower()
    assert "1" in env["verdict"]               # says how many it wanted

def test_help_verb_is_registered_and_lists_verbs():
    d = _load()
    env = d.dispatch(["help"])
    assert env["ok"] is True
    assert len(env["lines"]) >= 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: FAIL — `parse_command` / `dispatch` not defined.

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/mcp/dispatch.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: PASS (8 tests). `help` was registered in Task 1; Task 2 adds only the router.

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/dispatch.py tests/unit/test_mcp_dispatch.py
git commit -m "feat(dispatch): SSH_ORIGINAL_COMMAND routing, unknown verb lists the table"
```

---

### Task 3: `status` and `app.list`

**Files:**
- Modify: `scripts/mcp/dispatch.py`
- Test: `tests/unit/test_mcp_dispatch.py`

**Interfaces:**
- Consumes: `VerbSpec`, `envelope`, `VERBS`
- Produces: verbs `status`, `app.list`; helper `_load_manifest() -> Manifest`, `LIFECYCLE_CLASSES = ("ucc", "systemd")`

- [ ] **Step 1: Write the failing test**

```python
def test_app_list_returns_only_lifecycle_classes():
    d = _load()
    env = d.dispatch(["app.list"])
    assert env["ok"] is True
    # 18 ucc + 6 systemd = 24. Counted off App.class_ via the manifest loader,
    # NOT off the `# --- group ---` comment headers in apps.yaml, which have
    # drifted from the real class fields (postgres sits under the systemd
    # comment but is class: ucc). cron (10) and library (1) have no lifecycle.
    assert len(env["lines"]) == 24
    for line in env["lines"]:
        assert line.split()[1] in ("ucc", "systemd")

def test_app_list_names_the_class_so_the_phone_never_guesses_how_to_start_a_thing():
    d = _load()
    env = d.dispatch(["app.list"])
    joined = "\n".join(env["lines"])
    assert "sonarr ucc" in joined
    assert "listmonk systemd" in joined
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -k app_list -q`
Expected: FAIL — unknown verb `app.list`.

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/mcp/dispatch.py`, above `main()`:

```python
LIFECYCLE_CLASSES = ("ucc", "systemd")
_MANIFEST_PATH = HERE.parent.parent / "manifest" / "apps.yaml"


def _load_manifest():
    from lib import manifest as manifest_mod
    return manifest_mod.load(_MANIFEST_PATH)


def _verb_app_list(argv: List[str]) -> dict:
    started = time.time()
    man = _load_manifest()
    rows = []
    for name in sorted(man.apps()):
        app = man.app(name)
        if app.class_ in LIFECYCLE_CLASSES:
            rows.append("%s %s" % (name, app.class_))
    return envelope(verb="app.list", target=None, ok=True,
                    verdict="%d apps with a lifecycle" % len(rows),
                    lines=rows, elapsed_s=time.time() - started,
                    max_lines=MAX_LINES_CEILING)


def _verb_status(argv: List[str]) -> dict:
    """The Dashboard doc. Contract unchanged from Heartbeat v2 — app_status.py
    already emits exactly what the app expects, so this passes it through whole
    rather than re-wrapping it."""
    import subprocess
    started = time.time()
    proc = subprocess.run(
        [sys.executable, str(HERE / "app_status.py"), "--emit-json"],
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: PASS (10 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/dispatch.py tests/unit/test_mcp_dispatch.py
git commit -m "feat(dispatch): status passthrough and app.list with class badges"
```

---

### Task 4: Lifecycle verbs, gate-aware

**Files:**
- Modify: `scripts/mcp/dispatch.py`
- Test: `tests/unit/test_mcp_dispatch.py`

**Interfaces:**
- Consumes: `_load_manifest`, `LIFECYCLE_CLASSES`, `envelope`
- Produces: verbs `app.start`, `app.stop`, `app.restart`

**Why gate-awareness matters:** `lib/suppression.ucc_active()` reports the Ultra.cc maintenance gate. While it is up, `app-* start` is **blocked by Ultra.cc** — see `scripts/maint/lib/suppression.py:11` and `lib/kuma.py:454`. Without this check the phone shows a bare failure and the operator retries pointlessly.

- [ ] **Step 1: Write the failing test**

```python
def test_ucc_app_routes_to_the_approved_ultra_command(monkeypatch):
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    env = d.dispatch(["app.restart", "sonarr"])
    assert env["target"] == "sonarr"
    assert "ucc" in env["verdict"] or "app-sonarr" in "\n".join(env["lines"] + [env["verdict"]])

def test_systemd_app_routes_to_systemctl(monkeypatch):
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    env = d.dispatch(["app.restart", "listmonk"])
    assert env["target"] == "listmonk"
    assert "systemd" in env["verdict"] or "systemctl" in "\n".join(env["lines"] + [env["verdict"]])

def test_a_cron_app_is_refused_rather_than_silently_doing_nothing():
    d = _load()
    env = d.dispatch(["app.restart", "kometa"])
    assert env["ok"] is False
    assert "no lifecycle" in env["verdict"].lower()

def test_unknown_slug_is_refused():
    d = _load()
    env = d.dispatch(["app.restart", "not-an-app"])
    assert env["ok"] is False
    assert "unknown app" in env["verdict"].lower()

def test_start_during_the_ucc_gate_explains_itself(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_ucc_gate_up", lambda: True)
    env = d.dispatch(["app.start", "sonarr"])
    assert env["ok"] is False
    assert "gate" in env["verdict"].lower()

def test_restart_is_not_blocked_by_the_gate(monkeypatch):
    """Only `start` is gated by Ultra.cc. Blocking restart too would remove the
    operator's main remote remediation for no reason."""
    d = _load()
    monkeypatch.setenv("MANITOBA_DRY_RUN", "1")
    monkeypatch.setattr(d, "_ucc_gate_up", lambda: True)
    env = d.dispatch(["app.restart", "sonarr"])
    assert "gate" not in env["verdict"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -k "ucc or systemd or cron or slug or gate" -q`
Expected: FAIL — unknown verb `app.restart`.

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/mcp/dispatch.py`, above `main()`:

```python
def _ucc_gate_up() -> bool:
    """True when the Ultra.cc maintenance gate is active. While it is,
    Ultra.cc BLOCKS `app-* start` — so answering 'the gate is up' is more
    useful than relaying an opaque failure."""
    try:
        from lib import suppression
        return bool(suppression.ucc_active())
    except Exception:
        return False


def _lifecycle(verb_name: str, argv: List[str]) -> dict:
    from lib import lifecycle as lifecycle_mod
    started = time.time()
    slug = argv[0]
    man = _load_manifest()

    try:
        app = man.app(slug)
    except Exception:
        return envelope(verb=verb_name, target=slug, ok=False,
                        verdict="unknown app %r" % slug,
                        lines=["run app.list for the 24 apps that have a lifecycle"],
                        elapsed_s=time.time() - started)

    if app.class_ not in LIFECYCLE_CLASSES:
        return envelope(verb=verb_name, target=slug, ok=False,
                        verdict="%s is class %s and has no lifecycle"
                                % (slug, app.class_),
                        lines=["only ucc and systemd apps start/stop/restart"],
                        elapsed_s=time.time() - started)

    action = verb_name.split(".", 1)[1]
    if action == "start" and app.class_ == "ucc" and _ucc_gate_up():
        return envelope(verb=verb_name, target=slug, ok=False,
                        verdict="the Ultra.cc gate is up; `app-%s start` is blocked "
                                "until it clears" % slug,
                        lines=["restart is still available and is usually what you want"],
                        elapsed_s=time.time() - started)

    fn = {"start": lifecycle_mod.start,
          "stop": lifecycle_mod.stop,
          "restart": lifecycle_mod.restart}[action]
    res = fn(app)
    how = ("app-%s %s" % (app.raw.get("ucc_slug", slug), action)
           if app.class_ == "ucc"
           else "systemctl --user %s %s" % (action, app.raw.get("unit", slug)))
    return envelope(
        verb=verb_name, target=slug, ok=bool(res.ok),
        verdict="%s %s (%s: %s)" % (action, slug, app.class_, how)
                if res.ok else "%s %s FAILED: %s" % (action, slug, res.reason),
        lines=(res.stdout or "").splitlines() + (res.stderr or "").splitlines(),
        elapsed_s=time.time() - started)


for _a in ("start", "stop", "restart"):
    VERBS["app." + _a] = VerbSpec(
        handler=(lambda name: (lambda argv: _lifecycle(name, argv)))("app." + _a),
        arity=1, help="%s one app by slug (ucc or systemd)" % _a)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/dispatch.py tests/unit/test_mcp_dispatch.py
git commit -m "feat(dispatch): lifecycle verbs route by class and explain the UCC gate"
```

---

### Task 5: Wrapper verbs — `arr.search_wanted`, `unstick`, `logs`

**Files:**
- Modify: `scripts/mcp/dispatch.py`
- Test: `tests/unit/test_mcp_dispatch.py`

**Interfaces:**
- Consumes: `envelope`, `VerbSpec`
- Produces: `_run_mcp(script, args, timeout_s) -> tuple[bool, dict|None, str]`; verbs `arr.search_wanted`, `unstick`, `logs`

- [ ] **Step 1: Write the failing test**

```python
ARRS = ("sonarr", "sonarr2", "radarr", "radarr2")

def test_search_wanted_refuses_a_non_arr():
    d = _load()
    env = d.dispatch(["arr.search_wanted", "plex"])
    assert env["ok"] is False
    assert "not an *arr" in env["verdict"] or "not an arr" in env["verdict"].lower()

def test_search_wanted_accepts_each_arr(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: (True, {"per_arr": {}}, ""))
    for slug in ARRS:
        env = d.dispatch(["arr.search_wanted", slug])
        assert env["ok"] is True, slug
        assert env["target"] == slug

def test_logs_honours_an_explicit_tail_within_the_ceiling(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: (True, {"lines": [str(i) for i in range(300)]}, ""))
    env = d.dispatch(["logs", "sonarr", "--tail", "50"])
    assert len(env["lines"]) == 50

def test_logs_tail_cannot_exceed_the_ceiling(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_run_mcp",
                        lambda *a, **k: (True, {"lines": [str(i) for i in range(1000)]}, ""))
    env = d.dispatch(["logs", "sonarr", "--tail", "99999"])
    assert len(env["lines"]) == d.MAX_LINES_CEILING

def test_unstick_requires_both_slug_and_queue_id():
    d = _load()
    env = d.dispatch(["unstick", "sonarr"])
    assert env["ok"] is False
    assert "expects" in env["verdict"].lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -k "search_wanted or logs or unstick" -q`
Expected: FAIL — unknown verbs.

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/mcp/dispatch.py`, above `main()`:

```python
ARR_SLUGS = ("sonarr", "sonarr2", "radarr", "radarr2")


def _run_mcp(script: str, args: List[str], timeout_s: float = 60.0) -> tuple:
    """Run a sibling MCP script in --emit-json mode.

    Returns (ok, parsed_json_or_None, stderr). Those scripts always exit 0 in
    JSON mode by convention, so `ok` keys off parseable stdout, not the code.
    """
    import subprocess
    proc = subprocess.run([sys.executable, str(HERE / script)] + args,
                          capture_output=True, text=True, timeout=timeout_s)
    try:
        return (True, json.loads(proc.stdout), proc.stderr or "")
    except Exception:
        return (False, None, (proc.stderr or proc.stdout or "").strip())


def _verb_search_wanted(argv: List[str]) -> dict:
    started = time.time()
    slug = argv[0]
    if slug not in ARR_SLUGS:
        return envelope(verb="arr.search_wanted", target=slug, ok=False,
                        verdict="%s is not an *arr" % slug,
                        lines=["valid: " + ", ".join(ARR_SLUGS)],
                        elapsed_s=time.time() - started)
    ok, doc, err = _run_mcp("missing.py", ["--slug", slug, "--emit-json"], 120.0)
    return envelope(verb="arr.search_wanted", target=slug, ok=ok,
                    verdict="wanted search queued on %s" % slug if ok
                            else "search failed on %s" % slug,
                    lines=(json.dumps(doc, indent=2).splitlines() if doc
                           else err.splitlines()),
                    elapsed_s=time.time() - started)


def _verb_unstick(argv: List[str]) -> dict:
    started = time.time()
    slug, queue_id = argv[0], argv[1]
    ok, doc, err = _run_mcp(
        "unstick.py",
        ["--slug", slug, "--queue-id", str(queue_id), "--emit-json"], 90.0)
    return envelope(verb="unstick", target=slug, ok=ok,
                    verdict="unstuck %s queue item %s" % (slug, queue_id) if ok
                            else "unstick failed on %s" % slug,
                    lines=(json.dumps(doc, indent=2).splitlines() if doc
                           else err.splitlines()),
                    elapsed_s=time.time() - started)


def _verb_logs(argv: List[str]) -> dict:
    started = time.time()
    slug = argv[0]
    tail = MAX_LINES
    if "--tail" in argv:
        try:
            tail = int(argv[argv.index("--tail") + 1])
        except (ValueError, IndexError):
            tail = MAX_LINES
    ok, doc, err = _run_mcp("logs.py", ["--app", slug, "--emit-json"], 45.0)
    lines = (doc or {}).get("lines") or (err.splitlines() if err else [])
    return envelope(verb="logs", target=slug, ok=ok,
                    verdict="%s log tail" % slug if ok else "could not read %s log" % slug,
                    lines=lines, elapsed_s=time.time() - started, max_lines=tail)


VERBS["arr.search_wanted"] = VerbSpec(handler=_verb_search_wanted, arity=1,
                                      help="fire a wanted/missing search on one *arr")
VERBS["unstick"] = VerbSpec(handler=_verb_unstick, arity=2,
                            help="delete+blocklist a stuck queue item: unstick <slug> <queue-id>")
VERBS["logs"] = VerbSpec(handler=_verb_logs, arity=1,
                         help="tail one app's log: logs <slug> [--tail N]")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: PASS (21 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/dispatch.py tests/unit/test_mcp_dispatch.py
git commit -m "feat(dispatch): search-wanted, unstick and logs over the existing MCP scripts"
```

---

### Task 6: `arr_library_peek.py`

**Files:**
- Create: `scripts/mcp/arr_library_peek.py`
- Test: `tests/unit/test_mcp_arr_library_peek.py`

**Interfaces:**
- Produces: `peek(slug, client=None) -> dict` shaped `{"slug", "kind", "titles": [{"title", "have", "total", "complete"}], "ok", "error"}`; `kind` is `"series"` or `"movie"`

**Deliberately coarse** — the operator asked for "just a peek into the status," not library statistics.

- [ ] **Step 1: Write the failing test**

```python
import importlib.util, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def _load():
    path = REPO / "scripts" / "mcp" / "arr_library_peek.py"
    spec = importlib.util.spec_from_file_location("arr_library_peek", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["arr_library_peek"] = mod
    spec.loader.exec_module(mod)
    return mod

class FakeSonarr:
    """ArrClient exposes only get/post/put/delete — verified against
    scripts/mcp/lib/arr_client.py. Fakes mirror that, not a richer API."""
    def get(self, path, **kw):
        assert path == "/api/v3/series"
        return [{"title": "Show A", "statistics": {"episodeFileCount": 12,
                                                   "totalEpisodeCount": 30}},
                {"title": "Show B", "statistics": {"episodeFileCount": 10,
                                                   "totalEpisodeCount": 10}}]

class FakeRadarr:
    def get(self, path, **kw):
        assert path == "/api/v3/movie"
        return [{"title": "Movie A", "hasFile": True},
                {"title": "Movie B", "hasFile": False}]

def test_series_peek_reports_have_over_total():
    m = _load()
    out = m.peek("sonarr", client=FakeSonarr())
    a = [t for t in out["titles"] if t["title"] == "Show A"][0]
    assert (a["have"], a["total"], a["complete"]) == (12, 30, False)

def test_a_fully_present_series_is_marked_complete():
    m = _load()
    out = m.peek("sonarr", client=FakeSonarr())
    b = [t for t in out["titles"] if t["title"] == "Show B"][0]
    assert b["complete"] is True

def test_movie_peek_is_present_or_not():
    m = _load()
    out = m.peek("radarr", client=FakeRadarr())
    assert {t["title"]: t["complete"] for t in out["titles"]} == {
        "Movie A": True, "Movie B": False}
    for t in out["titles"]:
        assert t["total"] == 1

def test_peek_reports_no_consumption_data_whatsoever():
    """Privacy: content presence only. Nothing about who watched anything."""
    m = _load()
    out = m.peek("sonarr", client=FakeSonarr())
    banned = ("watch", "view", "session", "user", "played", "seen")
    blob = repr(out).lower()
    assert not any(b in blob for b in banned)

def test_a_dead_arr_degrades_that_slug_without_raising():
    m = _load()
    class Boom:
        def get(self, path, **kw): raise RuntimeError("connection refused")
    out = m.peek("sonarr", client=Boom())
    assert out["ok"] is False
    assert "connection refused" in out["error"]
    assert out["titles"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_arr_library_peek.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""scripts/mcp/arr_library_peek.py — coarse content-presence peek for one *arr.

Answers "do we have it", never "did anyone watch it" — see the privacy
constraint in docs/superpowers/specs/2026-08-03-qflix-admin-android-design.md.

Series  -> have/total episode counts per show.
Movies  -> present/absent per film (have/total are 1/1 or 0/1 so one shape
           serves both and the phone renders a single row type).

Deliberately coarse: the operator asked for a peek, not library statistics.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

SERIES_SLUGS = ("sonarr", "sonarr2")
MOVIE_SLUGS = ("radarr", "radarr2")


def _default_client(slug: str):
    from lib.arr_client import ArrClient
    return ArrClient(slug)


def peek(slug: str, client=None) -> dict:
    kind = "series" if slug in SERIES_SLUGS else "movie"
    out = {"slug": slug, "kind": kind, "titles": [], "ok": True, "error": ""}
    try:
        c = client if client is not None else _default_client(slug)
        if kind == "series":
            for s in c.get("/api/v3/series"):
                st = s.get("statistics") or {}
                have = int(st.get("episodeFileCount") or 0)
                total = int(st.get("totalEpisodeCount") or 0)
                out["titles"].append({
                    "title": s.get("title", "?"), "have": have, "total": total,
                    "complete": total > 0 and have >= total})
        else:
            for m in c.get("/api/v3/movie"):
                has = bool(m.get("hasFile"))
                out["titles"].append({
                    "title": m.get("title", "?"), "have": 1 if has else 0,
                    "total": 1, "complete": has})
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
        out["titles"] = []
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-json", action="store_true", required=True)
    ap.add_argument("--slug", required=True)
    args = ap.parse_args()
    json.dump(peek(args.slug), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_arr_library_peek.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/arr_library_peek.py tests/unit/test_mcp_arr_library_peek.py
git commit -m "feat(starr): coarse content-presence peek, with a test that it reports no consumption"
```

---

### Task 7: `arr_disk_usage.py`

**Files:**
- Create: `scripts/mcp/arr_disk_usage.py`
- Test: `tests/unit/test_mcp_arr_disk_usage.py`

**Interfaces:**
- Produces: `usage(slug, client=None) -> dict` shaped `{"slug", "bytes", "human", "title_count", "ok", "error"}`

- [ ] **Step 1: Write the failing test**

```python
import importlib.util, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

def _load():
    path = REPO / "scripts" / "mcp" / "arr_disk_usage.py"
    spec = importlib.util.spec_from_file_location("arr_disk_usage", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["arr_disk_usage"] = mod
    spec.loader.exec_module(mod)
    return mod

class FakeSonarr:
    def get(self, path, **kw):
        assert path == "/api/v3/series"
        return [{"statistics": {"sizeOnDisk": 1024 ** 3}},
                {"statistics": {"sizeOnDisk": 2 * 1024 ** 3}}]

class FakeRadarr:
    def get(self, path, **kw):
        assert path == "/api/v3/movie"
        return [{"sizeOnDisk": 5 * 1024 ** 3}, {"sizeOnDisk": 0}]

def test_series_usage_sums_size_on_disk():
    m = _load()
    out = m.usage("sonarr", client=FakeSonarr())
    assert out["bytes"] == 3 * 1024 ** 3
    assert out["title_count"] == 2

def test_movie_usage_sums_size_on_disk():
    m = _load()
    out = m.usage("radarr", client=FakeRadarr())
    assert out["bytes"] == 5 * 1024 ** 3

def test_human_is_a_short_string_a_phone_row_can_hold():
    m = _load()
    out = m.usage("sonarr", client=FakeSonarr())
    assert out["human"] == "3.0 GB"

def test_zero_bytes_is_reported_not_hidden():
    m = _load()
    class Empty:
        def get(self, path, **kw): return []
    out = m.usage("sonarr", client=Empty())
    assert out["ok"] is True and out["bytes"] == 0 and out["human"] == "0.0 B"

def test_a_dead_arr_degrades_without_raising():
    m = _load()
    class Boom:
        def get(self, path, **kw): raise RuntimeError("timed out")
    out = m.usage("sonarr", client=Boom())
    assert out["ok"] is False and "timed out" in out["error"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_arr_disk_usage.py -q`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""scripts/mcp/arr_disk_usage.py — bytes on disk managed by one *arr.

Sums the *arr's own sizeOnDisk rather than walking the filesystem: the box is a
shared seedbox with hardlinked seeding copies, so `du` would double-count what
the *arr considers one file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "maint"))

SERIES_SLUGS = ("sonarr", "sonarr2")


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f TB" % n


def _default_client(slug: str):
    from lib.arr_client import ArrClient
    return ArrClient(slug)


def usage(slug: str, client=None) -> dict:
    out = {"slug": slug, "bytes": 0, "human": "0.0 B",
           "title_count": 0, "ok": True, "error": ""}
    try:
        c = client if client is not None else _default_client(slug)
        total = 0
        if slug in SERIES_SLUGS:
            rows = c.get("/api/v3/series")
            for s in rows:
                total += int((s.get("statistics") or {}).get("sizeOnDisk") or 0)
        else:
            rows = c.get("/api/v3/movie")
            for m in rows:
                total += int(m.get("sizeOnDisk") or 0)
        out["bytes"] = total
        out["human"] = human(total)
        out["title_count"] = len(rows)
    except Exception as exc:
        out["ok"] = False
        out["error"] = str(exc)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--emit-json", action="store_true", required=True)
    ap.add_argument("--slug", required=True)
    args = ap.parse_args()
    json.dump(usage(args.slug), sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_arr_disk_usage.py -q`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/arr_disk_usage.py tests/unit/test_mcp_arr_disk_usage.py
git commit -m "feat(starr): per-arr disk usage from arr sizeOnDisk, not du (hardlinks)"
```

---

### Task 8: `starr` and `quota` verbs

**Files:**
- Modify: `scripts/mcp/dispatch.py`
- Test: `tests/unit/test_mcp_dispatch.py`

**Interfaces:**
- Consumes: `arr_library_peek.peek`, `arr_disk_usage.usage`, `envelope`
- Produces: verbs `starr`, `quota`

**One page, one round trip** — `starr` returns all four *arrs. Per-instance verbs would mean eight SSH handshakes to paint one screen over a flaky mobile link.

- [ ] **Step 1: Write the failing test**

```python
def test_starr_returns_all_four_arrs_in_one_call(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_peek_one",
                        lambda slug: {"slug": slug, "kind": "series",
                                      "titles": [], "ok": True, "error": ""})
    monkeypatch.setattr(d, "_usage_one",
                        lambda slug: {"slug": slug, "bytes": 0, "human": "0.0 B",
                                      "title_count": 0, "ok": True, "error": ""})
    env = d.dispatch(["starr"])
    assert env["ok"] is True
    assert sorted(env["arrs"]) == ["radarr", "radarr2", "sonarr", "sonarr2"]

def test_one_dead_arr_does_not_kill_the_page(monkeypatch):
    d = _load()
    def peek(slug):
        if slug == "radarr2":
            return {"slug": slug, "kind": "movie", "titles": [],
                    "ok": False, "error": "refused"}
        return {"slug": slug, "kind": "series", "titles": [], "ok": True, "error": ""}
    monkeypatch.setattr(d, "_peek_one", peek)
    monkeypatch.setattr(d, "_usage_one",
                        lambda slug: {"slug": slug, "bytes": 0, "human": "0.0 B",
                                      "title_count": 0, "ok": True, "error": ""})
    env = d.dispatch(["starr"])
    assert env["ok"] is True                       # page still renders
    assert env["arrs"]["radarr2"]["peek"]["ok"] is False
    assert "radarr2" in env["verdict"]

def test_quota_reports_used_total_and_percent(monkeypatch):
    d = _load()
    monkeypatch.setattr(d, "_quota_raw",
                        lambda: {"used_gb": 2190.0, "total_gb": 2794.0})
    env = d.dispatch(["quota"])
    assert env["ok"] is True
    assert env["used_gb"] == 2190.0
    assert env["percent"] == 78.4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -k "starr or quota" -q`
Expected: FAIL — unknown verbs.

- [ ] **Step 3: Write minimal implementation**

Append to `scripts/mcp/dispatch.py`, above `main()`:

```python
def _peek_one(slug: str) -> dict:
    ok, doc, err = _run_mcp("arr_library_peek.py", ["--slug", slug, "--emit-json"], 45.0)
    return doc if doc else {"slug": slug, "kind": "?", "titles": [],
                            "ok": False, "error": err}


def _usage_one(slug: str) -> dict:
    ok, doc, err = _run_mcp("arr_disk_usage.py", ["--slug", slug, "--emit-json"], 45.0)
    return doc if doc else {"slug": slug, "bytes": 0, "human": "0.0 B",
                            "title_count": 0, "ok": False, "error": err}


def _verb_starr(argv: List[str]) -> dict:
    """All four *arr rows in ONE call — see the spec's round-trip note."""
    started = time.time()
    arrs = {}
    degraded = []
    for slug in ARR_SLUGS:
        p = _peek_one(slug)
        u = _usage_one(slug)
        arrs[slug] = {"peek": p, "usage": u}
        if not p.get("ok") or not u.get("ok"):
            degraded.append(slug)
    env = envelope(
        verb="starr", target=None, ok=True,
        verdict="4 *arrs" if not degraded
                else "4 *arrs, degraded: " + ", ".join(degraded),
        lines=["%s %s %d titles" % (s, arrs[s]["usage"]["human"],
                                    len(arrs[s]["peek"]["titles"]))
               for s in ARR_SLUGS],
        elapsed_s=time.time() - started)
    env["arrs"] = arrs
    return env


def _quota_raw() -> dict:
    """Disk headroom. `quota -s` is the authority on this shared seedbox;
    df would report the whole array, not the slot."""
    import subprocess
    proc = subprocess.run(["quota", "-w"], capture_output=True, text=True, timeout=15)
    used_kb = total_kb = 0.0
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0].startswith("/dev/"):
            used_kb, total_kb = float(parts[1]), float(parts[2])
            break
    return {"used_gb": round(used_kb / 1024 / 1024, 1),
            "total_gb": round(total_kb / 1024 / 1024, 1)}


def _verb_quota(argv: List[str]) -> dict:
    started = time.time()
    raw = _quota_raw()
    used, total = raw["used_gb"], raw["total_gb"]
    pct = round(used / total * 100, 1) if total else 0.0
    env = envelope(verb="quota", target=None, ok=total > 0,
                   verdict="%.0f of %.0f GB used (%.1f%%)" % (used, total, pct)
                           if total else "could not read quota",
                   lines=[], elapsed_s=time.time() - started)
    env.update({"used_gb": used, "total_gb": total, "percent": pct})
    return env


VERBS["starr"] = VerbSpec(handler=_verb_starr, arity=0,
                          help="all four *arr rows (peek + disk) in one round trip")
VERBS["quota"] = VerbSpec(handler=_verb_quota, arity=0,
                          help="disk headroom for the Dashboard tile")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/unit/test_mcp_dispatch.py -q`
Expected: PASS (24 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/mcp/dispatch.py tests/unit/test_mcp_dispatch.py
git commit -m "feat(dispatch): starr in one round trip, quota tile, both degrade per-arr"
```

---

### Task 9: Key remint and provisioning

**Files:**
- Create: `scripts/configure/provision-admin-key.sh`
- Test: manual, on the box — this task mutates `authorized_keys`

**Interfaces:**
- Consumes: `scripts/mcp/dispatch.py` must exist and be executable on the box.

**This is the only task that changes live access.** Read every step before running any of it.

- [ ] **Step 1: Deploy and smoke-test the dispatcher BEFORE touching the key**

```bash
rsync -av scripts/mcp/dispatch.py scripts/mcp/arr_library_peek.py \
          scripts/mcp/arr_disk_usage.py \
          $BOX   # from secrets/seedbox.ssh-host:~/scripts/mcp/
ssh $BOX   # from secrets/seedbox.ssh-host 'chmod +x ~/scripts/mcp/dispatch.py && \
    python3 ~/scripts/mcp/dispatch.py help'
```

Expected: a JSON envelope listing every verb. If this fails, STOP — reminting the key against a broken dispatcher locks the phone out.

- [ ] **Step 2: Exercise every read-only verb over the CURRENT key**

```bash
for v in help app.list status starr quota; do
  echo "--- $v"
  ssh $BOX   # from secrets/seedbox.ssh-host "python3 ~/scripts/mcp/dispatch.py $v" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["ok"], d["verdict"])'
done
```

Expected: `True` and a sensible verdict for each. Fix anything that fails before continuing.

- [ ] **Step 3: Write the provisioning script**

```bash
#!/usr/bin/env bash
# scripts/configure/provision-admin-key.sh — mint the QFlix Admin phone key.
#
# Re-runnable. Mints a fresh ed25519 keypair, installs an authorized_keys entry
# that forces scripts/mcp/dispatch.py, and prints the bundle the phone needs.
#
# BLAST RADIUS: the operator accepted full blast radius 2026-08-03. The forced
# command is a MANIFEST of available actions, not a security boundary. The
# no-pty/no-forwarding/restrict flags are kept anyway - they cost nothing and
# remove whole classes of misuse that no verb needs.
set -uo pipefail

# Host comes from secrets/, never from this file - tests/unit/test_no_pii_in_repo.py
# treats a real hostname in a tracked file as a leak, and this repo is public.
BOX="${QFLIX_BOX:-$(cat "$(dirname "$0")/../../secrets/seedbox.ssh-host")}"
KEYDIR="${1:-./.admin-key}"
KEY="$KEYDIR/qflix-admin"

mkdir -p "$KEYDIR"
if [ -f "$KEY" ]; then
  echo "refusing to overwrite $KEY - move it aside first" >&2
  exit 2
fi

ssh-keygen -t ed25519 -N '' -C 'qflix-admin-phone' -f "$KEY" >/dev/null
PUB=$(cat "$KEY.pub")

OPTS='command="~/scripts/mcp/dispatch.py",no-pty,no-X11-forwarding'
OPTS="$OPTS,no-agent-forwarding,no-port-forwarding,restrict"

# Remove any prior qflix-admin-phone entry so re-running does not accumulate keys.
ssh "$BOX" "mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && \
  cp ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak-\$(date -u +%Y%m%dT%H%M%SZ) && \
  grep -v 'qflix-admin-phone' ~/.ssh/authorized_keys > ~/.ssh/ak.new || true; \
  printf '%s %s\n' '$OPTS' '$PUB' >> ~/.ssh/ak.new && \
  mv ~/.ssh/ak.new ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"

echo "=== host key pin (fetched over the authenticated channel, not keyscan) ==="
ssh "$BOX" "ssh-keyscan -t ed25519 localhost 2>/dev/null | sed 's/^localhost/seedbox.example.com/'"
echo "=== private key: $KEY  (load into the app, then delete this copy) ==="
```

- [ ] **Step 4: Run it**

```bash
chmod +x scripts/configure/provision-admin-key.sh
./scripts/configure/provision-admin-key.sh
```

Expected: a keypair in `./.admin-key/`, an `authorized_keys.bak-*` on the box, and a printed host-key line.

- [ ] **Step 5: Prove the new key works AND that the old key still does**

```bash
ssh -i ./.admin-key/qflix-admin $BOX   # from secrets/seedbox.ssh-host 'app.list' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["verdict"])'
```

Expected: `24 apps with a lifecycle`. The forced command means the requested
command is ignored in favour of `dispatch.py`, with `app.list` arriving via
`SSH_ORIGINAL_COMMAND`.

If this fails, restore: `ssh $BOX   # from secrets/seedbox.ssh-host 'cp ~/.ssh/authorized_keys.bak-<stamp> ~/.ssh/authorized_keys'`

- [ ] **Step 6: Confirm `.admin-key` cannot be committed**

```bash
grep -q '^\.admin-key/' .gitignore || echo '.admin-key/' >> .gitignore
git check-ignore -v .admin-key/qflix-admin
python -m pytest tests/unit/test_no_pii_in_repo.py -q
```

Expected: `check-ignore` names the rule; PII test passes.

- [ ] **Step 7: Commit**

```bash
git add scripts/configure/provision-admin-key.sh .gitignore
git commit -m "feat(admin): provisioning script for the phone key, forced to dispatch.py"
```

---

## Self-Review

**Spec coverage.** Dispatcher + envelope → Tasks 1–2. Verb table → Tasks 3–5, 8. `arr_library_peek.py` → Task 6. `arr_disk_usage.py` → Task 7. Key remint / `authorized_keys` → Task 9. Privacy constraint → Task 1 test plus Task 6 test. Blast-radius decision → recorded in the Task 1 docstring and the Task 9 script header. One-page-one-round-trip → Task 8. No `cron.run` → absent by construction; `app.list` returns only `ucc`/`systemd`, asserted in Task 3.

**Not covered here, by design:** the Android app (rename, drawer, three pages) is plan 2. It depends on this plan's wire format, which is why the envelope is fixed in Task 1 and every later task conforms to it.

**Type consistency.** `envelope()` keyword-only signature is used identically in Tasks 1–8. `LifecycleResult` fields (`ok`, `duration_s`, `stdout`, `stderr`, `reason`) match `scripts/maint/lib/lifecycle.py:37`. `App.class_` and `App.raw` match `scripts/maint/lib/manifest.py:101`. `peek()` and `usage()` return shapes are consumed unchanged by `_peek_one` / `_usage_one` in Task 8.

**Verified against the live box while writing this plan**, so no task rests on an assumption:

- `ArrClient` exposes **only** `get/post/put/delete` (`scripts/mcp/lib/arr_client.py:62-79`). Tasks 6 and 7 therefore call `c.get("/api/v3/series")` and `c.get("/api/v3/movie")` directly, and the test fakes mirror that same narrow surface rather than a richer invented one.
- `quota -w` on the box emits `/dev/sdaa1 2292465076 2929721344 2929721344 ...`, i.e. `parts[1]` is used-KB and `parts[2]` is the limit — which is exactly what `_quota_raw()` in Task 8 parses. 2292465076 KB / 2929721344 KB = 2186 / 2794 GB, matching the figure in the spec.

"""Manifest-driven canary WIRING guard - the durable enforcement of RULE 3.

"A guard that is committed but not scheduled is worse than no guard, because the
repo then reads as if the concern is covered."

That failure has now happened three times in this repo, each time caught only by
a human reading two lists side by side:

  - ucc-gate-stuck (06d4226) shipped inert in an earlier commit and had to be
    wired at five points afterwards.
  - sab-stall (2026-07-19) shipped with a manifest entry, a script and both
    systemd units - and ZERO installer wiring. It was never staged, never
    installed and never enabled. The repo read as if usenet stalls were covered.
  - tdarr-scanner and tdarr-healthcheck were added to the installer's unit-copy
    loop, its enable list and its smoke gate, but never to the tar STAGING list.
    Because that loop runs `cp -f` under `set -euo pipefail`, the FIRST missing
    source aborted the whole remote heredoc - which meant a fresh deploy stopped
    before installing ANY later unit (ucc-gate-stuck, dash-asset-integrity),
    before `daemon-reload`, and before every `enable --now` below it. Replayed
    verbatim on 2026-07-29: 49 of 60 units copied, exit 1, and none of the
    downstream canaries scheduled. Meanwhile Step 4.5 had ALREADY created their
    Kuma monitors, so the deploy manufactured permanently-red monitors with
    nothing pushing to them.

Every one of those is a list-vs-list mismatch inside one file, i.e. exactly what
a test can settle. This module derives the required wiring from
manifest/apps.yaml and asserts each of the five points the ucc-gate-stuck commit
established as the standard:

  (a) manifest entry                  -> the input, iterated below
  (b) both systemd units exist        -> scripts/maint/systemd/...{service,timer}
  (c) installer: script tar-staged, units tar-staged, unit in the cp loop,
      timer enabled, name in the smoke canary-timer loop
  (d) Kuma provisioning picks it up   -> covered by test_kuma_bootstrap*.py via
                                         manifest.canaries(); asserted here only
                                         as "not double-registered as a
                                         standalone self-pusher"
  (e) docs                            -> tests/unit/test_doc_counts.py

It also asserts the CLOSURE property that actually caused the abort: every unit
the installer tries to `cp` or `enable` must be tar-staged, whether or not it
belongs to a canary.
"""
import os
import re

from lib.manifest import load

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
APPS_YAML = os.path.join(REPO_ROOT, "manifest", "apps.yaml")
INSTALLER = os.path.join(REPO_ROOT, "scripts", "configure",
                         "240-maintenance-install.sh")
SYSTEMD_DIR = os.path.join(REPO_ROOT, "scripts", "maint", "systemd")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _canaries():
    return list(load(APPS_YAML).canaries())


def _installer():
    return _read(INSTALLER)


def _staged_paths():
    """Repo-relative paths handed to `tar -cf -` in Step 4.

    Matched by shape (a bare indented path ending in a line-continuation), which
    is what the tar argument list looks like and nothing else in the file does.
    """
    out = set()
    for line in _installer().split("\n"):
        m = re.match(r"^\s+((?:scripts|manifest)/[A-Za-z0-9._/-]+)\s*\\\s*$", line)
        if m:
            out.add(m.group(1))
    return out


def _cp_loop_units():
    """Unit filenames iterated by Step 7's `for unit in ... ; do cp -f ...`."""
    src = _installer()
    i = src.find("for unit in")
    assert i != -1, "Step 7 unit loop not found (installer restructured?)"
    seg = src[i:src.find("; do", i)]
    return set(re.findall(r"([A-Za-z0-9._-]+\.(?:service|timer))", seg))


def _enabled_units():
    """Every unit named by a `systemctl --user enable [--now] <unit>` line.

    Excludes the deliberate retirement of manitoba-maint-cp-upgrade.timer
    (folded into the window orchestrator 2026-06-28; the installer disables and
    deletes it, so it must NOT be staged).
    """
    out = set()
    for m in re.finditer(r"systemctl --user enable (?:--now )?([A-Za-z0-9._-]+\.(?:service|timer))",
                         _installer()):
        out.add(m.group(1))
    return out


def _smoke_loop_names():
    """Canary names in the install-time smoke `for canary in ...` gate."""
    m = re.search(r"for canary in ([^;]+); do", _installer())
    assert m, "smoke canary-timer loop not found (installer restructured?)"
    return set(m.group(1).split())


def _unit_stem(canary_name):
    return "manitoba-maint-canary-" + canary_name


# --- (b) units exist in the repo ------------------------------------------


def test_every_canary_has_both_systemd_units_in_the_repo():
    missing = []
    for c in _canaries():
        for ext in ("service", "timer"):
            f = _unit_stem(c.name) + "." + ext
            if not os.path.isfile(os.path.join(SYSTEMD_DIR, f)):
                missing.append(f)
    assert not missing, (
        "canaries with no systemd unit in scripts/maint/systemd/: %s" % sorted(missing))


def test_every_canary_script_exists_and_is_referenced_by_its_service_unit():
    """The unit's ExecStart must name the canary by its MANIFEST key, because
    that is the key cli.py looks up. A unit that runs the wrong name pushes to
    the wrong Kuma monitor (or none)."""
    problems = []
    for c in _canaries():
        script = os.path.join(REPO_ROOT, *c.script.split("/"))
        if not os.path.isfile(script):
            problems.append("%s: script %s missing" % (c.name, c.script))
        unit = os.path.join(SYSTEMD_DIR, _unit_stem(c.name) + ".service")
        if not os.path.isfile(unit):
            continue  # covered by the guard above
        body = _read(unit)
        if ("canary push " + c.name) not in body:
            problems.append("%s: unit ExecStart does not `canary push %s`"
                            % (c.name, c.name))
    assert not problems, "; ".join(problems)


# --- (c) installer wiring, all five points --------------------------------


def test_every_canary_script_is_tar_staged_by_the_installer():
    staged = _staged_paths()
    missing = [c.name for c in _canaries() if c.script not in staged]
    assert not missing, (
        "canary scripts never tar-staged in 240-maintenance-install.sh, so they "
        "never reach ~/scripts/canaries/ on the box: %s" % sorted(missing))


def test_every_canary_unit_is_tar_staged_by_the_installer():
    staged = _staged_paths()
    missing = []
    for c in _canaries():
        for ext in ("service", "timer"):
            rel = "scripts/maint/systemd/" + _unit_stem(c.name) + "." + ext
            if rel not in staged:
                missing.append(rel)
    assert not missing, (
        "canary units never tar-staged, so Step 7's `cp -f` cannot find them: %s"
        % sorted(missing))


def test_every_canary_unit_is_installed_by_the_step7_loop():
    loop = _cp_loop_units()
    missing = []
    for c in _canaries():
        for ext in ("service", "timer"):
            f = _unit_stem(c.name) + "." + ext
            if f not in loop:
                missing.append(f)
    assert not missing, (
        "canary units not copied into ~/.config/systemd/user by Step 7: %s"
        % sorted(missing))


def test_every_canary_timer_is_enabled_by_the_installer():
    enabled = _enabled_units()
    missing = [c.name for c in _canaries()
               if (_unit_stem(c.name) + ".timer") not in enabled]
    assert not missing, (
        "canary timers installed but never `systemctl --user enable`d - the "
        "script would sit on the box and never fire: %s" % sorted(missing))


def test_every_canary_is_in_the_install_smoke_timer_gate():
    names = _smoke_loop_names()
    missing = [c.name for c in _canaries() if c.name not in names]
    assert not missing, (
        "canaries absent from the install-time smoke canary-timer loop, so a "
        "missing timer would not fail the install gate: %s" % sorted(missing))


def test_smoke_timer_gate_has_no_unknown_canaries():
    """The reverse direction: a name in the smoke loop with no manifest entry
    would gate on a timer nothing installs, i.e. a permanent install failure."""
    known = {c.name for c in _canaries()}
    unknown = sorted(_smoke_loop_names() - known)
    assert not unknown, (
        "smoke canary-timer loop names canaries that are not in the manifest: %s"
        % unknown)


# --- the closure property that caused the 2026-07-29 abort ----------------


def test_every_unit_the_installer_touches_is_tar_staged():
    """`cp -f ~/scripts/maint/systemd/$unit` runs under `set -euo pipefail`, so
    ONE unit missing from the staging list aborts the entire remote heredoc -
    taking daemon-reload and every later `enable --now` with it. This is the
    exact defect that would have shipped dash-asset-integrity inert.

    Deliberate exception: manitoba-maint-cp-upgrade.timer, which the installer
    disables and REMOVES (retired 2026-06-28, folded into the window
    orchestrator), so it must not be staged and is not in the cp loop.
    """
    staged = {p.split("/")[-1] for p in _staged_paths()
              if p.startswith("scripts/maint/systemd/")}
    retired = {"manitoba-maint-cp-upgrade.timer"}
    touched = (_cp_loop_units() | _enabled_units()) - retired
    missing = sorted(touched - staged)
    assert not missing, (
        "units the installer copies or enables but never tar-stages - the first "
        "one aborts Step 7 under set -e and nothing after it is installed: %s"
        % missing)


def test_retired_cp_upgrade_timer_is_still_retired():
    """Pins the one exception above so it cannot silently become a real gap."""
    src = _installer()
    assert "disable --now manitoba-maint-cp-upgrade.timer" in src
    assert "rm -f ~/.config/systemd/user/manitoba-maint-cp-upgrade.timer" in src
    assert "manitoba-maint-cp-upgrade.timer" not in _cp_loop_units()


# --- (d) Kuma provisioning ------------------------------------------------


def test_no_canary_is_double_registered_as_a_standalone_self_pusher():
    """Manifest canaries are provisioned by bootstrap-kuma-monitors.py straight
    from manifest.canaries(), and lib/kuma.py folds them into the audit's
    expected set. Adding one to STANDALONE_SELF_PUSH_MONITORS as well would mint
    a duplicate token key and falsify the documented NON_MANIFEST_MONITORS
    enumeration in tests/unit/test_doc_counts.py."""
    from lib import kuma
    standalone = set(getattr(kuma, "STANDALONE_SELF_PUSH_MONITORS", []))
    dupes = sorted(c.kuma_monitor for c in _canaries()
                   if c.kuma_monitor in standalone)
    assert not dupes, (
        "manifest canaries also listed in lib/kuma.py "
        "STANDALONE_SELF_PUSH_MONITORS: %s" % dupes)


def test_every_canary_declares_a_kuma_monitor_and_a_schedule():
    bad = [c.name for c in _canaries()
           if not (c.kuma_monitor or "").strip() or not (c.schedule or "").strip()]
    assert not bad, "canaries missing kuma_monitor or schedule: %s" % sorted(bad)

"""scripts/migrate/*.sh -- the box-2 migration, driven with fake ssh/scp/rsync (QFLX-39).

Every network tool the scripts can reach (ssh, scp, rsync, curl, sleep) is a
stub on PATH that appends {tool, host, cmd} as a JSON line to a log and answers
from a small rule table. The tests then assert on WHICH remote commands were
issued, on WHICH host, in WHICH order -- the only things that matter for a
migration orchestrator. What is pinned:

  * I-3 inert by default: no --execute = no ssh/scp/rsync at all, and the plan
    names every step; a re-run prints the identical plan (I-4, no diff);
  * the window guard: OLD_HOST profile ultra inside its window refuses (exit 2)
    before ANY mutating command; an unresolvable profile refuses (I-12);
  * manifest-driven tables: the native-installer and appdata tables follow a
    fixture manifest, not a pasted list;
  * 50-cutover stops on the first failure and prints COMPLETED steps, never
    reaching the later ones; I-1 (mute blue before green loud; hold blue comms
    before releasing green's) and I-5 (disarm blue before anything on green's
    gate; green never armed by the cutover) ordering;
  * the freeze snapshot is taken once and is what rollback resumes (no
    hashes=all); 55-rollback mutes green before re-enabling anything on blue
    and re-arms blue only after green is confirmed disarmed.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
MIG = REPO / "scripts" / "migrate"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")

FAKE = r'''
import json, os, sys
tool, args = sys.argv[1], sys.argv[2:]
data = ""
try:
    if not sys.stdin.isatty():
        data = sys.stdin.read()
except Exception:
    data = ""
host, cmd = "", " ".join(args)
if tool == "ssh":
    i = 0
    while i < len(args):
        if args[i] == "-o":
            i += 2
            continue
        if args[i].startswith("-"):
            i += 1
            continue
        break
    host = args[i] if i < len(args) else ""
    cmd = " ".join(args[i + 1:])
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"tool": tool, "host": host, "cmd": cmd, "stdin": data[:300]}) + "\n")
for r in json.loads(os.environ.get("FAKE_RULES") or "[]"):
    if r.get("tool", tool) != tool or r.get("host", host) != host:
        continue
    if r["match"] in cmd:
        sys.stdout.write(r.get("out", ""))
        sys.exit(int(r.get("rc", 0)))
if "hostpolicy.py preflight" in cmd:
    sys.stdout.write(os.environ.get("FAKE_PROFILE", "generic") + "\n")
    sys.exit(0)
if "hostpolicy.py in-window" in cmd:
    sys.exit(int(os.environ.get("FAKE_INWINDOW_RC", "1")))
sys.exit(0)
'''

SIBLING = """#!/bin/sh
printf '{"tool": "sibling", "host": "", "cmd": "%s %s", "stdin": ""}\\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
case "$(basename "$0")" in
  30-*) exit ${SIB30_RC:-0} ;;
  35-*) exit ${SIB35_RC:-0} ;;
  40-*) exit ${SIB40_RC:-0} ;;
esac
exit 0
"""

BLUE, GREEN = "blue", "green"
MONDAY_NOON = "2026-10-12T12:00:00Z"     # a Monday, inside the Ultra window
TUESDAY_NOON = "2026-10-13T12:00:00Z"
SNAP_JSON = '{"qbit": ["aaa111", "bbb222"], "sab_was_paused": false}'


def _env(tmp: Path, rules=(), profile="generic", inwindow_rc=1, now=TUESDAY_NOON, **extra) -> dict:
    stub = tmp / "stubbin"
    stub.mkdir(exist_ok=True)
    fake = tmp / "fake_tool.py"
    fake.write_text(FAKE, encoding="utf-8")
    py = Path(sys.executable).as_posix()
    for tool in ("ssh", "scp", "rsync", "curl"):
        p = stub / tool
        p.write_text('#!/bin/sh\nexec "%s" "%s" %s "$@"\n' % (py, fake.as_posix(), tool), newline="\n")
        p.chmod(0o755)
    sl = stub / "sleep"
    sl.write_text("#!/bin/sh\nexit 0\n", newline="\n")
    sl.chmod(0o755)
    sib = tmp / "siblings"
    sib.mkdir(exist_ok=True)
    for name in ("30-sync-media.sh", "35-sync-appdata.sh", "40-validate-green.sh"):
        (sib / name).write_text(SIBLING, newline="\n")
    secrets = tmp / "secrets"
    secrets.mkdir(exist_ok=True)
    for name, val in (("postgres.port", "42009"), ("sonarr.port", "8989"), ("sonarr.key", "k")):
        (secrets / name).write_text(val + "\n")
    env = dict(os.environ)
    env.update({
        "PATH": stub.as_posix() + os.pathsep + env.get("PATH", ""),
        "STUB_LOG": (tmp / "calls.log").as_posix(),
        "FAKE_RULES": json.dumps(list(rules)),
        "FAKE_PROFILE": profile,
        "FAKE_INWINDOW_RC": str(inwindow_rc),
        "QFLIX_NOW": now,
        "QFLIX_MIGRATE_PYTHON": py,
        "QFLIX_MIGRATE_STATE_DIR": (tmp / "mstate").as_posix(),
        "QFLIX_GREEN_SECRETS_DIR": (tmp / "green-secrets").as_posix(),
        "QFLIX_MIGRATE_SIBLINGS": sib.as_posix(),
        "SECRETS_DIR": secrets.as_posix(),
        "QFLIX_HEALTH_TRIES": "2",
        "QFLIX_HEALTH_SLEEP": "0",
        "HOME": (tmp / "home").as_posix(),
    })
    (tmp / "home").mkdir(exist_ok=True)
    env.update(extra)
    return env


def _run(tmp: Path, script: str, *args, env=None, **kw):
    env = env or _env(tmp, **kw)
    r = subprocess.run(["bash", (MIG / script).as_posix(), *args], env=env, cwd=str(tmp),
                       capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    log = tmp / "calls.log"
    calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    return r, calls


def _remote(calls):
    return [c for c in calls if c["tool"] in ("ssh", "scp", "rsync")]


def _idx(calls, pred):
    for i, c in enumerate(calls):
        if pred(c):
            return i
    return -1


# --- syntax -------------------------------------------------------------------

@pytest.mark.parametrize("script", sorted(p.name for p in MIG.glob("*.sh")))
def test_bash_syntax(script):
    r = subprocess.run(["bash", "-n", (MIG / script).as_posix()], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_scripts_are_lf_and_ascii():
    for p in list(MIG.glob("*.sh")) + list(MIG.glob("*.py")) + [MIG / "migrate.conf"]:
        raw = p.read_bytes()
        assert b"\r\n" not in raw, p.name
        raw.decode("ascii")


def test_no_host_literal_in_migrate_files():
    """Public repo: hosts come from arguments or gitignored secrets only. No
    ssh-style user-at-FQDN target, and no IPv4 literal but loopback (the Ultra docker
    gateway is asked of hostpolicy_ultra, never written here)."""
    import re
    for p in MIG.iterdir():
        if not p.is_file() or p.suffix not in (".sh", ".py", ".md", ".conf"):
            continue
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"[A-Za-z0-9_-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text), p.name
        ips = set(re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text)) - {"127.0.0.1"}
        assert not ips, (p.name, ips)


# --- I-3: inert by default ------------------------------------------------------

@pytest.mark.parametrize("script", ["15-bootstrap-new.sh", "20-install-stack.sh",
                                    "35-sync-appdata.sh", "50-cutover.sh", "55-rollback.sh"])
def test_dry_run_makes_no_remote_call(tmp_path, script):
    r, calls = _run(tmp_path, script, GREEN, "--old-host", BLUE)
    assert r.returncode == 0, r.stderr
    assert _remote(calls) == []
    assert "dry-run" in r.stdout


@pytest.mark.parametrize("script", ["15-bootstrap-new.sh", "20-install-stack.sh",
                                    "35-sync-appdata.sh", "50-cutover.sh", "55-rollback.sh"])
def test_dry_run_rerun_is_identical(tmp_path, script):
    a, _ = _run(tmp_path, script, GREEN, "--old-host", BLUE)
    b, _ = _run(tmp_path, script, GREEN, "--old-host", BLUE)
    assert a.returncode == b.returncode == 0
    assert a.stdout == b.stdout


def test_cutover_plan_names_every_step(tmp_path):
    r, _ = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE)
    for n, word in enumerate(["freeze blue", "media delta", "appdata", "validate green",
                              "single pager", "gate", "front door", "park blue comms"], 1):
        assert "  %d. %s" % (n, word) in r.stdout, word
    assert "--arm-green-gate" in r.stdout


def test_rollback_plan_names_every_step(tmp_path):
    r, _ = _run(tmp_path, "55-rollback.sh", GREEN, "--old-host", BLUE)
    for n in range(1, 7):
        assert "  %d. " % n in r.stdout
    assert r.stdout.index("mute green") < r.stdout.index("blue loud")


def test_missing_new_host_is_usage_exit_2(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh")
    assert r.returncode == 2
    assert "STAGE=usage" in r.stderr
    assert _remote(calls) == []


# --- window guard (spec 5.6) ------------------------------------------------------

LIVE = [
    ("00-preflight.sh", []),
    ("15-bootstrap-new.sh", ["--execute"]),
    ("30-sync-media.sh", []),
    ("35-sync-appdata.sh", ["--execute"]),
    ("40-validate-green.sh", []),
    ("50-cutover.sh", ["--execute", "--yes"]),
    ("55-rollback.sh", ["--execute", "--yes"]),
]
MUTATING_MARKERS = ("python3 - pause", "python3 - mute", "python3 - loud", "rm -f ~/.config",
                    "rsync -aH", "git clone", "crontab -", "printf 'generic", "resume")


@pytest.mark.parametrize("script,extra", LIVE)
def test_live_run_refused_inside_ultra_window(tmp_path, script, extra):
    r, calls = _run(tmp_path, script, GREEN, "--old-host", BLUE, *extra,
                    profile="ultra", now=MONDAY_NOON)
    assert r.returncode == 2, (r.stdout, r.stderr)
    assert "STAGE=maintenance-window" in r.stderr
    for c in _remote(calls):
        assert not any(m in c["cmd"] for m in MUTATING_MARKERS), c


def test_ultra_window_is_evaluated_from_hostpolicy_not_the_box(tmp_path):
    # ultra: the box is only asked for its PROFILE; the window is code
    # (hostpolicy_ultra), so the box's in-window answer is never consulted.
    r, calls = _run(tmp_path, "40-validate-green.sh", GREEN, "--old-host", BLUE,
                    profile="ultra", now=MONDAY_NOON, inwindow_rc=1)
    assert r.returncode == 2
    assert not any("in-window" in c["cmd"] for c in calls)


def test_generic_old_host_window_is_asked_of_the_box(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    profile="generic", inwindow_rc=0)
    assert r.returncode == 2
    assert "STAGE=maintenance-window" in r.stderr
    assert any(c["host"] == BLUE and "hostpolicy.py in-window" in c["cmd"] for c in calls)


def test_unresolvable_old_profile_refuses(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    profile="")
    assert r.returncode == 2
    assert "STAGE=old-host-profile-unresolved" in r.stderr
    assert not any("freeze.py" in c["stdin"] or "python3 - " in c["cmd"] for c in calls)


def test_outside_window_proceeds(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    profile="ultra", now=TUESDAY_NOON,
                    rules=[{"match": "python3 - snapshot", "out": SNAP_JSON}])
    assert "STAGE=maintenance-window" not in r.stderr
    assert any(c["cmd"] == "python3 - snapshot" for c in calls)


# --- manifest-driven tables -------------------------------------------------------

FIXTURE_MANIFEST = """\
apps:
  sonarr:
    class: ucc
    ucc_slug: sonarr
    health: {kind: http_api, port_secret: sonarr.port}
  seerr:
    class: ucc
    ucc_slug: seerr
    health: {kind: http_api, port_secret: seerr.port}
  bazarr2:
    class: systemd
    unit: bazarr2.service
    health: {kind: http_api, port_secret: bazarr2.port}
  kometa:
    class: cron
    unit: kometa.service
"""


def _native_rows(stdout):
    import re
    return [m.group(2) for m in re.finditer(r"^  \[(MISSING|native )\] (\S+)", stdout, re.M)]


def test_install_stack_table_follows_the_manifest(tmp_path):
    man = tmp_path / "apps.yaml"
    man.write_text(FIXTURE_MANIFEST)
    r, _ = _run(tmp_path, "20-install-stack.sh", GREEN, "--old-host", BLUE,
                QFLIX_MIGRATE_MANIFEST=man.as_posix())
    assert r.returncode == 0, r.stderr
    assert _native_rows(r.stdout) == ["seerr", "sonarr"]          # ever-UCC apps only, sorted


def test_install_stack_native_table_equals_real_manifest(tmp_path):
    r, _ = _run(tmp_path, "20-install-stack.sh", GREEN, "--old-host", BLUE)
    apps = yaml.safe_load((REPO / "manifest" / "apps.yaml").read_text(encoding="utf-8"))["apps"]
    want = sorted(k for k, v in apps.items() if isinstance(v, dict) and v.get("ucc_slug"))
    assert _native_rows(r.stdout) == want


def test_install_stack_refuses_execute_with_missing_installers(tmp_path):
    empty = tmp_path / "configure"
    empty.mkdir()
    r, calls = _run(tmp_path, "20-install-stack.sh", GREEN, "--old-host", BLUE, "--execute",
                    QFLIX_CONFIGURE_DIR=empty.as_posix())
    assert "[MISSING]" in r.stdout, (r.stdout, r.stderr)
    assert r.returncode == 1
    assert "STAGE=native-installer-missing" in r.stderr
    assert _remote(calls) == []                      # refused before touching anything


def test_appdata_table_follows_the_manifest(tmp_path):
    man = tmp_path / "apps.yaml"
    man.write_text(FIXTURE_MANIFEST)
    r, _ = _run(tmp_path, "35-sync-appdata.sh", GREEN, "--old-host", BLUE,
                QFLIX_MIGRATE_MANIFEST=man.as_posix())
    assert r.returncode == 0, r.stderr
    a_lines = [line.split()[2].rstrip(":") for line in r.stdout.splitlines() if "[PLAN] A " in line]
    assert a_lines == ["bazarr2", "seerr", "sonarr"]


def test_appdata_disarms_green_before_any_data_lands(tmp_path):
    man = tmp_path / "apps.yaml"
    man.write_text(FIXTURE_MANIFEST)
    rules = [{"host": GREEN, "match": "get(\"armed\")", "out": "False\n"}]
    r, calls = _run(tmp_path, "35-sync-appdata.sh", GREEN, "--old-host", BLUE, "--execute",
                    rules=rules, QFLIX_MIGRATE_MANIFEST=man.as_posix())
    rc = _remote(calls)
    disarm = _idx(rc, lambda c: c["host"] == GREEN and c["cmd"].startswith("rm -f ~/.config/systemd/user/manitoba-maint-entitlement"))
    roster = _idx(rc, lambda c: c["host"] == GREEN and 'd["armed"] = False' in c["cmd"])
    first_copy = _idx(rc, lambda c: c["host"] == BLUE and c["cmd"].startswith("bash -s -- .apps/"))
    assert -1 < disarm < roster < first_copy, r.stderr
    # every sqlite-tree app of the fixture manifest is copied, nothing else
    # argv: bash -s -- SRC EXCLUDES TAG GREEN DST; state trees carry TAG state-*
    copied = sorted({c["cmd"].split()[3].split("/")[1] for c in rc
                     if c["host"] == BLUE and c["cmd"].startswith("bash -s -- .apps/")
                     and not c["cmd"].split()[5].startswith("state-")})
    assert copied == ["bazarr2", "seerr", "sonarr"]
    # blue's swap state is archived beside green's live swap dir, never into it
    swap = [c["cmd"] for c in rc if c["host"] == BLUE and c["cmd"].startswith("bash -s -- .opt/maint/swap ")]
    assert swap and swap[0].split()[-1] == ".opt/maint/swap.from-blue"


def test_unplaceable_manifest_app_fails_closed(tmp_path):
    man = tmp_path / "apps.yaml"
    man.write_text(FIXTURE_MANIFEST + "  mystery:\n    class: ucc\n    ucc_slug: mystery\n")
    r, _ = _run(tmp_path, "35-sync-appdata.sh", GREEN, "--old-host", BLUE,
                QFLIX_MIGRATE_MANIFEST=man.as_posix())
    assert r.returncode == 2
    assert "STAGE=manifest-table" in r.stderr


# --- 50-cutover: ordering + stop on first failure ---------------------------------

GATE_RULES = [
    {"host": BLUE, "match": "then echo armed", "out": "armed\n"},
    {"host": BLUE, "match": "cat ~/.config/systemd/user/manitoba-maint-entitlement.service.d/execute.conf",
     "out": "[Service]\nExecStart=\nExecStart=/usr/bin/python3 %h/x --execute\n"},
    {"match": "python3 - snapshot", "out": SNAP_JSON},
]


def test_cutover_success_orders_i1_and_i5(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=GATE_RULES)
    assert r.returncode == 0, r.stderr
    rc = _remote(calls)
    # I-1: blue muted (Kuma + webhook) before green goes loud.
    blue_mute = _idx(rc, lambda c: c["host"] == BLUE and c["cmd"] == "python3 - mute")
    blue_park = _idx(rc, lambda c: c["host"] == BLUE and "discord-webhook.url.held; fi; test ! -f" in c["cmd"])
    green_loud = _idx(rc, lambda c: c["host"] == GREEN and c["cmd"] == "python3 - loud")
    assert -1 < blue_mute < green_loud and -1 < blue_park < green_loud
    # I-1: blue's comms held before green's are released.
    blue_hold = _idx(rc, lambda c: c["host"] == BLUE and "disable --now qflix-newsletter.timer" in c["cmd"])
    green_rel = _idx(rc, lambda c: c["host"] == GREEN and "enable --now qflix-newsletter.timer" in c["cmd"])
    assert -1 < blue_hold < green_rel
    # I-5: blue disarmed before any gate command reaches green; green never armed.
    blue_disarm = _idx(rc, lambda c: c["host"] == BLUE and c["cmd"].startswith("rm -f ~/.config/systemd/user/manitoba-maint-entitlement"))
    first_green_gate = _idx(rc, lambda c: c["host"] == GREEN and "manitoba-maint-entitlement" in c["cmd"])
    assert -1 < blue_disarm < first_green_gate
    assert not any(c["host"] == GREEN and "ExecStart" in c["cmd"] for c in rc)
    # the freeze paused exactly the snapshot, and the snapshot was recorded for 55
    pause = [c for c in rc if c["cmd"].startswith("python3 - pause")]
    assert len(pause) == 1 and "aaa111" in pause[0]["cmd"]
    assert (tmp_path / "mstate" / "freeze-snapshot.json").read_text().strip() == SNAP_JSON
    assert (tmp_path / "mstate" / "blue-gate-dropin.conf").exists()
    # health gate ran AFTER the front-door step, as the post-flip check
    sib = [c["cmd"] for c in calls if c["tool"] == "sibling"]
    assert sib[-1].startswith("40-validate-green.sh") and "--post" in sib[-1]
    assert "CUTOVER COMPLETE" in r.stderr


def test_cutover_stops_on_first_failure_and_lists_completed(tmp_path):
    rules = GATE_RULES + [{"host": BLUE, "match": "python3 - mute", "rc": 1}]
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=rules)
    assert r.returncode == 1
    assert "STAGE=mute-blue" in r.stderr
    assert "COMPLETED: 1-freeze-blue 2-media-delta 3-appdata 4-validate-green" in r.stderr
    rc = _remote(calls)
    assert not any(c["cmd"] == "python3 - loud" for c in rc)          # step 5b never ran
    assert not any("manitoba-maint-entitlement" in c["cmd"] for c in rc)  # step 6 never ran


def test_cutover_validate_failure_stops_before_any_pager_change(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=GATE_RULES, SIB40_RC="1")
    assert r.returncode == 1
    assert "STAGE=validate-green" in r.stderr
    assert "COMPLETED: 1-freeze-blue 2-media-delta 3-appdata" in r.stderr
    assert not any(c["cmd"] in ("python3 - mute", "python3 - loud") for c in _remote(calls))


def test_health_gate_failure_names_rollback(tmp_path):
    # 40 passes in pre mode (step 4) but fails every --post try (step 7).
    sib = tmp_path / "siblings"
    env = _env(tmp_path, rules=GATE_RULES)
    (sib / "40-validate-green.sh").write_text(
        SIBLING.replace("40-*) exit ${SIB40_RC:-0} ;;",
                        '40-*) case "$*" in *--post*) exit 1 ;; esac; exit 0 ;;'), newline="\n")
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes", env=env)
    assert r.returncode == 1
    assert "STAGE=health-gate" in r.stderr and "55-rollback" in r.stderr
    assert "6b-green-confirmed-disarmed" in r.stderr
    posts = [c for c in calls if c["tool"] == "sibling" and "--post" in c["cmd"]]
    assert len(posts) == 2                                               # QFLIX_HEALTH_TRIES
    assert not any("enable --now qflix-newsletter.timer" in c["cmd"] for c in _remote(calls))


def test_cutover_rerun_keeps_the_first_freeze_snapshot(tmp_path):
    env = _env(tmp_path, rules=GATE_RULES)
    _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes", env=env)
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--execute", "--yes", env=env)
    assert r.returncode == 0, r.stderr
    assert sum(1 for c in calls if c["cmd"] == "python3 - snapshot") == 1


def test_arm_green_gate_refuses_while_blue_armed(tmp_path):
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--arm-green-gate",
                    "--execute", "--yes", rules=GATE_RULES)
    assert r.returncode == 1
    assert "STAGE=gate-order" in r.stderr
    assert not any(c["host"] == GREEN and "ExecStart" in c["cmd"] for c in calls)


def test_arm_green_gate_after_blue_disarmed(tmp_path):
    rules = [{"host": BLUE, "match": "then echo armed", "out": "disarmed\n"}]
    r, calls = _run(tmp_path, "50-cutover.sh", GREEN, "--old-host", BLUE, "--arm-green-gate",
                    "--execute", "--yes", rules=rules)
    assert r.returncode == 0, r.stderr
    rc = _remote(calls)
    check = _idx(rc, lambda c: c["host"] == BLUE and "then echo armed" in c["cmd"])
    arm = _idx(rc, lambda c: c["host"] == GREEN and "ExecStart" in c["cmd"])
    assert -1 < check < arm


# --- 55-rollback ------------------------------------------------------------------

def _seed_cutover_state(tmp: Path, armed=True):
    st = tmp / "mstate"
    st.mkdir(exist_ok=True)
    (st / "freeze-snapshot.json").write_text(SNAP_JSON + "\n")
    if armed:
        (st / "blue-gate-dropin.conf").write_text("[Service]\nExecStart=\nExecStart=/x --execute\n")
    else:
        (st / "blue-gate-was-disarmed").write_text("")


GREEN_DISARMED = [{"host": GREEN, "match": "then echo armed", "out": "disarmed\n"}]


def test_rollback_orders_green_mute_first_and_gate_last(tmp_path):
    _seed_cutover_state(tmp_path)
    r, calls = _run(tmp_path, "55-rollback.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=GREEN_DISARMED)
    assert r.returncode == 0, r.stderr
    rc = _remote(calls)
    green_mute = _idx(rc, lambda c: c["host"] == GREEN and c["cmd"] == "python3 - mute")
    green_hold = _idx(rc, lambda c: c["host"] == GREEN and "disable --now qflix-newsletter.timer" in c["cmd"])
    green_disarm = _idx(rc, lambda c: c["host"] == GREEN and c["cmd"].startswith("rm -f ~/.config"))
    blue_loud = _idx(rc, lambda c: c["host"] == BLUE and c["cmd"] == "python3 - loud")
    blue_resume = _idx(rc, lambda c: c["host"] == BLUE and c["cmd"].startswith("python3 - resume"))
    blue_comms = _idx(rc, lambda c: c["host"] == BLUE and "enable --now qflix-newsletter.timer" in c["cmd"])
    blue_rearm = _idx(rc, lambda c: c["host"] == BLUE and "cat > ~/.config" in c["cmd"])
    first_blue_write = min(i for i in (blue_loud, blue_resume, blue_comms, blue_rearm) if i > -1)
    assert -1 < green_mute < first_blue_write and -1 < green_hold < first_blue_write
    assert -1 < green_disarm < blue_rearm
    assert blue_loud < blue_resume < blue_comms < blue_rearm
    # resume = exactly the snapshot, never hashes=all
    assert "aaa111" in rc[blue_resume]["cmd"] and "hashes=all" not in json.dumps(calls)
    assert "ExecStart=/x --execute" in rc[blue_rearm]["stdin"]
    assert "ROLLBACK COMPLETE" in r.stderr


def test_rollback_refuses_without_freeze_snapshot(tmp_path):
    r, calls = _run(tmp_path, "55-rollback.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=GREEN_DISARMED)
    assert r.returncode == 2
    assert "STAGE=freeze-snapshot-missing" in r.stderr
    assert "COMPLETED: 1-green-muted 2-green-confirmed-disarmed 3-blue-loud" in r.stderr
    assert not any(c["cmd"].startswith("python3 - resume") for c in calls)


def test_rollback_leaves_blue_gate_off_when_it_was_off(tmp_path):
    _seed_cutover_state(tmp_path, armed=False)
    r, calls = _run(tmp_path, "55-rollback.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=GREEN_DISARMED)
    assert r.returncode == 0, r.stderr
    assert not any(c["host"] == BLUE and "cat > ~/.config" in c["cmd"] for c in calls)
    assert "6-blue-gate-was-disarmed-left-off" in r.stderr


def test_rollback_never_rearms_blue_if_green_still_armed(tmp_path):
    _seed_cutover_state(tmp_path)
    rules = [{"host": GREEN, "match": "then echo armed", "out": "armed\n"}]
    r, calls = _run(tmp_path, "55-rollback.sh", GREEN, "--old-host", BLUE, "--execute", "--yes",
                    rules=rules)
    assert r.returncode == 1
    assert "STAGE=green-disarm" in r.stderr
    assert not any(c["host"] == BLUE and ("cat > ~/.config" in c["cmd"] or c["cmd"] == "python3 - loud")
                   for c in calls)

"""QFLX-63: a rebuilt box must come back with everything the live box runs.

Box-verified 2026-10-21: three media-janitor timer pairs (reaper,
audio-disposition, unknown-codec-stream), stream_stats.py, listmonk-sync.py,
two crontab lines and most of scripts/mcp were LIVE on the box as hand copies
that no installer shipped. deploy-drift compares deployed bytes to git, so a
hand copy reads green until the day the box is rebuilt (or migrated to the Gold
slot) and the job silently is not there.

These tests are GENERIC on purpose. Each previous fix of this class added the
missing line and nothing else, and the class came back. Here the required set
is derived from the ledger (manifest/jobs.yaml) and from the scripts' own
imports, so a new job or a new import is covered the day it lands:

  1. every jobs.yaml `timer:` is staged + copied + enabled by some installer
     (strictly: tar list + Step 7 loop + `enable --now` when that is 240);
  2. every jobs.yaml `cron:` job has an installer that writes its line, with
     the declared schedule;
  3. every `lib.X` import and every exec'd ~/scripts/mcp script of a maint
     script 240 ships resolves to a file 240 also stages AND copies;
  4. every .py a 59a wrapper execs is shipped beside it.
Plus the behaviour of scripts/lib/cron.sh (the cron writer) and the reaper
arming boundary (repo unit dry-run; the box-only drop-in is never shipped).
"""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
JOBS = REPO / "manifest" / "jobs.yaml"
SYSTEMD = REPO / "scripts" / "maint" / "systemd"
I240 = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
I59A = REPO / "scripts" / "configure" / "59a-plex-stream-crons-install.sh"
CRON_LIB = REPO / "scripts" / "lib" / "cron.sh"

# The two crontab lines exactly as they stand on the box (2026-10-21). The
# installer must write these bytes: cron_ensure matches the whole line, so any
# difference would append a second, duplicate job instead of a no-op.
BOX_CRON_LINES = (
    "0 4 * * * /usr/bin/python3 /home/quadstronaut/scripts/ops/listmonk-sync.py"
    " >> /home/quadstronaut/.apps/listmonk/logs/sync.log 2>&1",
    "15 * * * * /usr/bin/python3 /home/quadstronaut/scripts/maint/arr-housekeeping.py"
    " --unstick >> /home/quadstronaut/.opt/maint/arr-unstick.log 2>&1",
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _installers() -> dict[str, str]:
    out = {}
    for d in ("scripts/configure", "scripts/install"):
        for p in sorted((REPO / d).rglob("*.sh")):
            out[p.name] = _read(p)
    return out


def _jobs() -> dict:
    return (yaml.safe_load(_read(JOBS)) or {}).get("jobs", {})


def _code_lines(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


# --- 240 list parsers (same shapes test_canary_wiring.py pins) ---------------

def _staged_240() -> set[str]:
    out = set()
    for line in _read(I240).splitlines():
        m = re.match(r"^\s+((?:scripts|manifest)/[A-Za-z0-9._/-]+)\s*\\\s*$", line)
        if m:
            out.add(m.group(1))
    return out


def _cp_loop_240() -> set[str]:
    src = _read(I240)
    i = src.find("for unit in")
    assert i != -1, "Step 7 unit loop not found"
    return set(re.findall(r"([A-Za-z0-9._-]+\.(?:service|timer))", src[i:src.find("; do", i)]))


def _copied_240(rel: str) -> bool:
    return f'"$STG"/{rel} ' in _read(I240)


# --- 1. timers ---------------------------------------------------------------

def _timer_jobs() -> list[tuple[str, str, str]]:
    """[(job, timer filename, service filename)] for every `timer:` entry."""
    out = []
    for name, e in _jobs().items():
        if not isinstance(e, dict) or not e.get("timer"):
            continue
        tpath = REPO / e["timer"]
        tname = tpath.name
        svc = tname[:-len(".timer")] + ".service"
        if tpath.is_file():
            m = re.search(r"^Unit=(\S+)", _read(tpath), re.M)
            if m:
                svc = m.group(1)
        out.append((name, tname, svc))
    return out


def _enables(text: str, unit: str) -> bool:
    rx = re.compile(r"systemctl --user enable\b[^\n'\"]*?(?<![\w.-])" + re.escape(unit) + r"(?![\w.-])")
    return any(rx.search(ln) for ln in _code_lines(text))


def _ships(text: str, unit: str) -> bool:
    """Named on a code line that is not the enable line: a tar list entry, a
    cp/scp/cat-redirect source, or a for-unit loop member."""
    rx = re.compile(r"(?<![\w.-])" + re.escape(unit) + r"(?![\w.-])")
    return any(rx.search(ln) and "enable" not in ln for ln in _code_lines(text))


def test_timer_collector_finds_the_ledger():
    names = {t for _, t, _ in _timer_jobs()}
    assert len(names) >= 50, len(names)
    assert "manitoba-maint-reaper.timer" in names


@pytest.mark.parametrize("job,timer,service", _timer_jobs())
def test_every_jobs_timer_is_staged_copied_and_enabled(job, timer, service):
    # Expanded, so `enable --now "$LCS_TIMER"` counts as enabling that timer.
    owners = [n for n, t in _installers().items()
              if _enables(_expand(t), timer) and _ships(t, timer) and _ships(t, service)]
    assert owners, (
        "jobs.yaml %s: no installer ships %s + %s AND enables the timer. The "
        "unit can only be on the box as a hand copy, which a rebuild drops." %
        (job, timer, service))
    if "240-maintenance-install.sh" in owners:
        # 240's own pipeline, all three points: tar-stage -> Step 7 cp loop ->
        # enable --now. Naming a unit anywhere in the file is not enough (a
        # comment-adjacent mention passed review for the reaper for months).
        staged, loop = _staged_240(), _cp_loop_240()
        for u in (timer, service):
            assert "scripts/maint/systemd/" + u in staged, "240 does not tar-stage %s" % u
            assert u in loop, "240 Step 7 loop does not install %s" % u
        assert re.search(r'systemctl --user enable --now "?' + re.escape(timer) + r'\b',
                         _expand(_read(I240))), (
            "240 enables %s without --now: the schedule would not start until "
            "the next login/boot" % timer)


# --- 2. cron -----------------------------------------------------------------

def _expand(text: str) -> str:
    """Substitute simple shell assignments (NAME="${NAME:-v}", NAME='v',
    NAME="v") so a line built from variables can be matched."""
    vals = {}
    for m in re.finditer(r'^\s*([A-Z_][A-Z0-9_]*)="\$\{\1:-([^}]*)\}"', text, re.M):
        vals[m.group(1)] = m.group(2)
    for m in re.finditer(r"""^\s*([A-Z_][A-Z0-9_]*)=(['"])([^'"\n]*)\2\s*(?:#.*)?$""", text, re.M):
        vals.setdefault(m.group(1), m.group(3))
    for m in re.finditer(r"^\s*([A-Z_][A-Z0-9_]*)=([\w./@-]+)\s*(?:#.*)?$", text, re.M):
        vals.setdefault(m.group(1), m.group(2))
    for _ in range(2):
        for k, v in vals.items():
            text = text.replace("${%s}" % k, v)
            text = re.sub(r"\$%s(?![A-Za-z0-9_])" % k, lambda _m, v=v: v, text)
    return text


def _cron_jobs() -> list[tuple[str, str, str]]:
    return [(n, e["cron"], str(e.get("schedule", "")))
            for n, e in _jobs().items() if isinstance(e, dict) and e.get("cron")]


def test_cron_collector_finds_the_ledger():
    jobs = {n for n, _, _ in _cron_jobs()}
    assert {"cron-listmonk-sync", "cron-arr-housekeeping-unstick"} <= jobs


@pytest.mark.parametrize("job,cmd,schedule", _cron_jobs())
def test_every_jobs_cron_line_is_written_by_an_installer(job, cmd, schedule):
    assert schedule, "jobs.yaml %s declares no schedule" % job
    rx = re.compile(r"(?<![\d*/,-])" + re.escape(schedule) + r" [^\n]*" + re.escape(cmd))
    owners = []
    for n, t in _installers().items():
        if "crontab" not in t and "cron_ensure" not in t:
            continue
        if any(rx.search(ln) for ln in _code_lines(_expand(t))):
            owners.append(n)
    assert owners, (
        "jobs.yaml %s (%s %s): no installer writes this crontab line, so it "
        "exists on the box only by hand and a rebuild drops it" % (job, schedule, cmd))


@pytest.mark.parametrize("line", BOX_CRON_LINES)
def test_box_cron_lines_are_written_verbatim(line):
    text = _read(I240)
    assert "'%s'" % line in text, "240 must carry the box's line byte-for-byte: %s" % line


# --- 3. mcp imports + exec'd mcp scripts --------------------------------------

def _lib_imports(path: Path) -> set[str]:
    out = set()
    for n in ast.walk(ast.parse(_read(path))):
        if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            if n.module == "lib":
                out |= {a.name for a in n.names}
            elif n.module.startswith("lib."):
                out.add(n.module.split(".")[1])
        elif isinstance(n, ast.Import):
            out |= {a.name.split(".")[1] for a in n.names if a.name.startswith("lib.")}
    return out


def _mcp_execs(path: Path) -> set[str]:
    src = _read(path)
    out = set(re.findall(r"_run_mcp\(\s*[\"']([\w-]+\.py)[\"']", src))
    out |= set(re.findall(r"[\"']mcp[\"']\s*/\s*[\"']([\w-]+\.py)[\"']", src))
    return out


def _shipped_240_py() -> list[str]:
    return sorted(p for p in _staged_240()
                  if re.fullmatch(r"scripts/(maint|mcp)/[\w-]+\.py", p)
                  or re.fullmatch(r"scripts/mcp/lib/\w+\.py", p))


def test_mcp_exec_collector_finds_known_callers():
    found = set()
    for rel in _shipped_240_py():
        found |= _mcp_execs(REPO / rel)
    assert {"collect.py", "logs.py", "unstick.py", "missing.py"} <= found, found


@pytest.mark.parametrize("rel", _shipped_240_py())
def test_every_lib_import_of_a_240_script_resolves_to_a_shipped_file(rel):
    staged = _staged_240()
    problems = []
    for mod in sorted(_lib_imports(REPO / rel)):
        cands = ["scripts/maint/lib/%s.py" % mod, "scripts/mcp/lib/%s.py" % mod]
        hit = [c for c in cands if (REPO / c).is_file()]
        if not hit:
            problems.append("lib.%s: no such file in scripts/{maint,mcp}/lib" % mod)
        elif not any(c in staged for c in hit):
            problems.append("lib.%s: %s never tar-staged by 240" % (mod, hit[0]))
        elif hit[0].startswith("scripts/mcp/") and not _copied_240(hit[0]):
            # maint/lib is copied wholesale (cp -rf); mcp/lib file by file.
            problems.append("lib.%s: %s staged but never copied out" % (mod, hit[0]))
    assert not problems, "%s: %s" % (rel, "; ".join(problems))


@pytest.mark.parametrize("rel", _shipped_240_py())
def test_every_mcp_script_a_240_script_execs_is_shipped(rel):
    staged = _staged_240()
    missing = [s for s in sorted(_mcp_execs(REPO / rel))
               if "scripts/mcp/" + s not in staged or not _copied_240("scripts/mcp/" + s)]
    assert not missing, "%s execs ~/scripts/mcp/%s, which 240 does not stage+copy" % (rel, missing)


def _chmod_755_targets(text: str) -> set[str]:
    """Paths named by every `chmod 755 ...` command, continuations joined."""
    joined = re.sub(r"\\\n\s*", " ", text)
    out = set()
    for m in re.finditer(r"^\s*chmod 755 ([^\n]+)$", joined, re.M):
        out |= set(m.group(1).split())
    return out


def test_240_restores_the_mcp_exec_bit():
    targets = _chmod_755_targets(_read(I240))
    missing = sorted(p for p in _staged_240()
                     if p.startswith("scripts/mcp/") and "~/" + p not in targets)
    assert not missing, (
        "copied but not chmod 755'd (git mode 100755; a Windows checkout tars "
        "644 and deploy-drift flags deploy-mode-drift): %s" % missing)


# --- 4. 59a wrappers ---------------------------------------------------------

def test_59a_ships_every_py_its_wrappers_exec():
    text = _read(I59A)
    m = re.search(r"for f in ([^;]+); do\s*\n\s*scpm_to", text)
    assert m, "59a ship loop not found"
    shipped = set(m.group(1).split())
    missing = []
    for sh in sorted(f for f in shipped if f.endswith(".sh")):
        for py in re.findall(r"\$HERE/([\w-]+\.py)", _read(REPO / "scripts" / "plex" / sh)):
            if py not in shipped:
                missing.append("%s -> %s" % (sh, py))
    assert not missing, "59a ships a wrapper without the .py it execs: %s" % missing
    for f in shipped:
        assert "~/scripts/plex/" + f in text.split("chmod +x", 1)[-1], "59a never chmods %s" % f


# --- reaper arming boundary ----------------------------------------------------

def test_repo_reaper_unit_ships_dry_run():
    body = _read(SYSTEMD / "manitoba-maint-reaper.service")
    execs = re.findall(r"^ExecStart=(.*)$", body, re.M)
    assert execs and all("--execute" not in e for e in execs), execs


def test_no_installer_ships_or_touches_the_reaper_drop_in():
    """The box arms the reaper with manitoba-maint-reaper.service.d/. Shipping
    or overwriting it from the repo would either arm a fresh box blind or
    disarm the live one."""
    for n, t in _installers().items():
        bad = [ln for ln in _code_lines(t) if "reaper.service.d" in ln]
        assert not bad, "%s touches the reaper drop-in: %s" % (n, bad)
    assert not (SYSTEMD / "manitoba-maint-reaper.service.d").exists(), (
        "the reaper's arming drop-in is box-only and must not be tracked")


# --- scripts/lib/cron.sh behaviour ----------------------------------------------

CRONTAB_STUB = r'''#!/usr/bin/env bash
# File-backed crontab: -l prints it (exit 1 when absent), "-" replaces it.
f="$QFLIX_TEST_CRONTAB"
case "$1" in
  -l) [ -f "$f" ] || { echo "no crontab for user" >&2; exit 1; }; cat "$f" ;;
  -)  cat > "$f" ;;
  *)  exit 2 ;;
esac
'''

LM = BOX_CRON_LINES[0]
UN = BOX_CRON_LINES[1]


@pytest.fixture
def cron_env(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("needs bash")
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "crontab").write_text(CRONTAB_STUB, newline="\n")
    tab = tmp_path / "crontab.txt"

    def run(*calls: tuple[str, str, str], remote: bool = False) -> subprocess.CompletedProcess:
        fn = "cron_ensure_remote" if remote else "cron_ensure"
        body = [
            'set -euo pipefail',
            f'chmod +x "{stub.as_posix()}/crontab"',
            f'export CRONTAB="$(cd "{stub.as_posix()}" && pwd)/crontab"',
            f'export QFLIX_TEST_CRONTAB="{tab.as_posix()}"',
            # On-host sshm shape from scripts/lib/ssh.sh.
            'sshm() { bash -c "$*"; }',
            f'source "{CRON_LIB.as_posix()}"',
        ]
        for tag, line, match in calls:
            body.append("%s %s %s %s" % (fn, _sq(tag), _sq(line), _sq(match)))
        script = tmp_path / "run.sh"
        script.write_text("\n".join(body) + "\n", newline="\n")
        return subprocess.run(["bash", script.as_posix()], capture_output=True,
                              text=True, timeout=60)

    return run, tab


def _sq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _active(tab: Path) -> list[str]:
    return [ln for ln in tab.read_text().splitlines() if ln and not ln.startswith("#")]


def test_cron_ensure_installs_into_an_empty_crontab(cron_env):
    run, tab = cron_env
    r = run(("listmonk-sync", LM, "listmonk-sync[.]py"))
    assert r.returncode == 0, r.stderr
    assert _active(tab) == [LM]
    assert "[qflix-cron:listmonk-sync]" in tab.read_text()


def test_cron_ensure_is_a_byte_for_byte_noop_on_the_live_box(cron_env):
    """The box already holds both lines, hand-added and unmarked. A re-run must
    not rewrite the crontab at all (no marker, no reorder, no duplicate)."""
    run, tab = cron_env
    live = "MAILTO=\"\"\n* * * * * /home/quadstronaut/scripts/plex/stream_stats.sh >/dev/null 2>&1\n%s\n%s\n" % (LM, UN)
    tab.write_text(live, newline="\n")
    r = run(("listmonk-sync", LM, "listmonk-sync[.]py"),
            ("arr-unstick", UN, "arr-housekeeping[.]py --unstick"), remote=True)
    assert r.returncode == 0, r.stderr
    assert tab.read_text() == live
    assert r.stdout.count("present, unchanged") == 2


def test_cron_ensure_twice_is_idempotent(cron_env):
    run, tab = cron_env
    for _ in range(2):
        assert run(("arr-unstick", UN, "arr-housekeeping[.]py --unstick")).returncode == 0
    assert _active(tab) == [UN]
    assert tab.read_text().count("[qflix-cron:arr-unstick]") == 1


def test_cron_ensure_replaces_a_drifted_line_and_keeps_the_rest(cron_env):
    run, tab = cron_env
    other = "15 3 * * * /usr/bin/python3 /home/quadstronaut/scripts/maint/arr-housekeeping.py --report"
    tab.write_text("# operator note\n%s\n15 * * * * python3 ~/scripts/maint/arr-housekeeping.py --unstick\n" % other,
                   newline="\n")
    r = run(("arr-unstick", UN, "arr-housekeeping[.]py --unstick"))
    assert r.returncode == 0, r.stderr
    assert _active(tab) == [other, UN]
    assert "# operator note" in tab.read_text()


def test_cron_ensure_leaves_a_swap_hold_alone(cron_env):
    """312 --swap comments listmonk writers out under #QFLX-37-HOLD#; an
    installer run mid-swap must not re-add an active copy."""
    run, tab = cron_env
    held = "#QFLX-37-HOLD# " + LM + "\n"
    tab.write_text(held, newline="\n")
    r = run(("listmonk-sync", LM, "listmonk-sync[.]py"), remote=True)
    assert r.returncode == 0, r.stderr
    assert tab.read_text() == held
    assert "held by a swap" in r.stdout


def test_cron_ensure_remote_round_trips_the_line_exactly(cron_env):
    """The line crosses one shell boundary (printf %q -> bash -s); `*` and `>>`
    must arrive unglobbed and unredirected."""
    run, tab = cron_env
    r = run(("listmonk-sync", LM, "listmonk-sync[.]py"), remote=True)
    assert r.returncode == 0, r.stderr
    assert _active(tab) == [LM]

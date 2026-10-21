"""lib/deploy_parity.py - deployed manifest / appctl / units vs a commit (QFLX-20, O-6).

A real throwaway git repo plays origin/master; a tmp dir plays $HOME.
deploy-drift goes red when the deployed manifest differs from the commit.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lib import deploy_parity

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

REPO = Path(__file__).resolve().parents[2]

FILES = {
    "manifest/apps.yaml": "apps: {a: {class: ucc}}\n",
    "manifest/jobs.yaml": "jobs: []\n",
    "scripts/lib/appctl": "#!/usr/bin/env bash\necho hi\n",
    "scripts/maint/systemd/manitoba-maint-pusher.service": "[Service]\nExecStart=/x\n",
    "scripts/maint/systemd/qflix-collect.timer": "[Timer]\n",
}


def _git(repo, *a):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                   check=True, capture_output=True)


@pytest.fixture
def world(tmp_path):
    repo = tmp_path / "src"
    for rel, body in FILES.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode())
    subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "scripts/lib/appctl")
    _git(repo, "commit", "-qm", "init")
    home = tmp_path / "home"
    (home / ".opt" / "maint").mkdir(parents=True)
    (home / "bin").mkdir()
    (home / ".config" / "systemd" / "user").mkdir(parents=True)
    dep = {"manifest/apps.yaml": home / ".opt/maint/apps.yaml",
           "manifest/jobs.yaml": home / ".opt/maint/jobs.yaml",
           "scripts/lib/appctl": home / "bin/appctl"}
    for rel in ("scripts/maint/systemd/manitoba-maint-pusher.service",
                "scripts/maint/systemd/qflix-collect.timer"):
        dep[rel] = home / ".config/systemd/user" / Path(rel).name
    for rel, dst in dep.items():
        shutil.copyfile(repo / rel, dst)
    (home / "bin" / "appctl").chmod(0o755)
    return repo, home, dep


def _cmp(repo, home):
    return deploy_parity.compare(str(repo), "master", home)


def test_matching_deploy_is_clean(world):
    repo, home, _ = world
    r = _cmp(repo, home)
    assert r["drift"] == [] and r["match"] == 5


def test_deployed_manifest_that_differs_from_the_commit_is_drift(world):
    repo, home, dep = world
    dep["manifest/apps.yaml"].write_text("apps: {a: {class: systemd}}\n")
    assert _cmp(repo, home)["drift"] == ["manifest/apps.yaml:differs"]


def test_stale_appctl_and_edited_unit_are_drift(world):
    repo, home, dep = world
    dep["scripts/lib/appctl"].write_text("old\n")
    dep["scripts/maint/systemd/manitoba-maint-pusher.service"].write_text("[Service]\nExecStart=/y\n")
    assert sorted(_cmp(repo, home)["drift"]) == [
        "scripts/lib/appctl:differs",
        "scripts/maint/systemd/manitoba-maint-pusher.service:differs"]


@pytest.mark.skipif(os.name != "posix", reason="exec bit")
def test_appctl_losing_its_exec_bit_is_drift(world):
    repo, home, dep = world
    dep["scripts/lib/appctl"].chmod(0o644)
    assert _cmp(repo, home)["drift"] == ["scripts/lib/appctl:not-executable"]


def test_missing_deployed_manifest_is_drift_not_a_pass(world):
    repo, home, dep = world
    dep["manifest/jobs.yaml"].unlink()
    assert _cmp(repo, home)["drift"] == ["manifest/jobs.yaml:missing"]


def test_deployed_only_unit_and_foreign_units_are_not_this_legs_business(world):
    repo, home, _ = world
    u = home / ".config/systemd/user"
    (u / "qflix-native-x.service").write_text("new unit with no git render\n")
    (u / "somebody-else.service").write_text("x\n")
    (u / "qflix-collect.service.d").mkdir()
    assert _cmp(repo, home)["drift"] == []


def test_unresolvable_ref_raises_never_reads_clean(world):
    repo, home, _ = world
    with pytest.raises(RuntimeError):
        deploy_parity.compare(str(repo), "origin/nonexistent", home)
    assert deploy_parity.main(["--src", str(repo), "--ref", "nope", "--home", str(home)]) == 2


def test_cli_exit_codes(world, capsys):
    repo, home, dep = world
    assert deploy_parity.main(["--src", str(repo), "--ref", "master", "--home", str(home)]) == 0
    dep["manifest/apps.yaml"].write_text("x\n")
    assert deploy_parity.main(["--src", str(repo), "--ref", "master", "--home", str(home)]) == 1
    assert "STAGE=deploy-parity-drift" in capsys.readouterr().err


def test_deploy_drift_canary_invokes_the_parity_leg_and_stays_valid_bash():
    sh = (REPO / "scripts" / "canaries" / "deploy-drift.sh").read_text(encoding="utf-8")
    assert "deploy_parity.py" in sh and 'show "$REF:scripts/maint/lib/deploy_parity.py"' in sh
    # The whole remote script sits inside sshm '...': an apostrophe would end it.
    body = sh.split("RES=$(sshm '", 1)[1].split("') || RC=$?", 1)[0]
    assert "'" not in body
    if shutil.which("bash"):
        assert subprocess.run(["bash", "-n", str(REPO / "scripts/canaries/deploy-drift.sh")]).returncode == 0


def test_installer_stages_every_new_file():
    inst = (REPO / "scripts/configure/240-maintenance-install.sh").read_text(encoding="utf-8")
    for f in ("scripts/maint/lib/swapstate.py", "scripts/maint/lib/runtime_parity.py",
              "scripts/maint/lib/deploy_parity.py", "scripts/ops/qflix-listen-set.sh",
              "scripts/ops/ucc-uninstall.sh"):
        assert f + " \\" in inst, f
    assert "~/scripts/ops/qflix-listen-set.sh" in inst
    assert "~/scripts/ops/ucc-uninstall.sh" in inst


def test_staged_lib_modules_import_only_staged_lib_modules():
    """QFLX-55: 240 stages lib/*.py from an explicit list. A new module the
    pusher imports but 240 does not ship crashes the daemon on the box at import
    time (dormant_stop.py nearly went out that way). Close the list over imports."""
    import re
    inst = (REPO / "scripts/configure/240-maintenance-install.sh").read_text(encoding="utf-8")
    staged = set(re.findall(r"scripts/maint/lib/(\w+)\.py \\", inst))
    pat = re.compile(r"^\s*(?:from lib import ([\w ,]+?)(?:\s+as\s+\w+)?\s*$|from lib\.(\w+) import|import lib\.(\w+))",
                     re.M)
    missing = {}
    for mod in sorted(staged):
        src = (REPO / "scripts/maint/lib" / f"{mod}.py").read_text(encoding="utf-8")
        for names, dotted, imp in pat.findall(src):
            targets = [dotted or imp] if (dotted or imp) else [n.split(" as ")[0].strip() for n in names.split(",")]
            for t in targets:
                if t and (REPO / "scripts/maint/lib" / f"{t}.py").exists() and t not in staged:
                    missing.setdefault(mod, []).append(t)
    assert not missing, f"staged module imports an unstaged lib module (add it to 240): {missing}"


def test_socket_units_are_compared_too(world):
    """QFLX-28: qflix-prowlarr-fwd.socket is a deployed unit like any service."""
    repo, home, _ = world
    rel = "scripts/maint/systemd/qflix-prowlarr-fwd.socket"
    (repo / rel).write_bytes(b"[Socket]\nListenStream=127.0.0.1:1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "socket")
    dst = home / ".config/systemd/user/qflix-prowlarr-fwd.socket"
    dst.write_bytes(b"[Socket]\nListenStream=0.0.0.0:1\n")
    assert _cmp(repo, home)["drift"] == [f"{rel}:differs"]
    dst.write_bytes((repo / rel).read_bytes())
    assert _cmp(repo, home)["drift"] == []



# -- QFLX-52: configure/ scripts are compared only when an installer ships them --

def test_ship_rule_counts_copies_to_configure_and_nothing_else():
    text = "\n".join([
        'cp -f   "$STG"/scripts/configure/a.sh ~/scripts/configure/a.sh',
        '    cp -f "$STG"/x/b.py $HOME/scripts/configure/b.py   # indented, trailing comment',
        'install -m 755 src/c.sh ${HOME}/scripts/configure/c.sh',
        'rm -f ~/scripts/configure/dead.sh',          # decommission, not shipping
        '      ~/scripts/configure/cont.sh \\',        # rm continuation line
        '# cp -f x ~/scripts/configure/commented.sh',  # comment
        'chmod +x ~/scripts/configure/a.sh',
        'sshm "~/scripts/configure/run.sh --execute"',  # invoking, not shipping
    ])
    assert deploy_parity.shipped_configure_from_text(text) == {"a.sh", "b.py", "c.sh"}


def test_shipped_configure_reads_installers_at_the_ref_not_the_worktree(world):
    repo, _, _ = world
    inst = repo / "scripts/configure/240-maintenance-install.sh"
    inst.parent.mkdir(parents=True)
    inst.write_text('cp -f "$STG"/scripts/configure/240-maintenance-install.sh '
                    '~/scripts/configure/240-maintenance-install.sh\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "installer")
    # An uncommitted edit must not change the answer: the canary judges the ref.
    inst.write_text(inst.read_text() + 'cp -f x ~/scripts/configure/wip.sh\n')
    assert deploy_parity.shipped_configure(str(repo), "master") == {"240-maintenance-install.sh"}


def test_shipped_configure_cli_refuses_an_empty_set_and_a_bad_ref(world, capsys):
    repo, _, _ = world             # fixture repo has no installer: nothing ships
    assert deploy_parity.main(["--src", str(repo), "--ref", "master", "--shipped-configure"]) == 2
    assert "parser-broken" in capsys.readouterr().err
    assert deploy_parity.main(["--src", str(repo), "--ref", "nope", "--shipped-configure"]) == 2


def test_real_installers_ship_the_box_run_configure_scripts_only():
    """Pin today's derivation against the real tree: the box-run scripts are
    shipped, the QFLX-52 hand copies (workstation-run) are not."""
    text = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                     for d in deploy_parity.INSTALLER_DIRS if (REPO / d).is_dir()
                     for p in sorted((REPO / d).rglob("*")) if p.is_file())
    shipped = deploy_parity.shipped_configure_from_text(text)
    # 59b is box-run (plexapi venv, entitlement runbook): shipped, or it reads unshipped.
    assert {"240-maintenance-install.sh", "55-kometa-install.sh",
            "59b-plex-welcome-library.py",
            "300-native-unpackerr-install.sh", "312-native-postgres-install.sh"} <= shipped
    assert not shipped & {"80-vlogs-install.sh", "90-sabnzbd-usenet-install.sh",
                          "91-nginx-root-to-dash.sh"}
    # Every shipped name is a real file, or the canary would compare against nothing.
    assert all((REPO / "scripts/configure" / n).is_file() for n in shipped)


def test_drift_scope_constants_match_the_canary_walk():
    """deploy_parity mirrors deploy-drift.sh's find globs and skip list so tests
    can derive the covered set; pin the two so they cannot drift apart."""
    sh = (REPO / "scripts/canaries/deploy-drift.sh").read_text(encoding="utf-8")
    globs = " -o ".join(f'-name "*{s}"' for s in deploy_parity.DRIFT_SUFFIXES)
    assert f'find "$DEPLOYED" -type f \\( {globs} \\)' in sh
    skip = "|".join(f"*{p}/*" for p in deploy_parity.DRIFT_SKIP_PARTS)
    assert f"    {skip}) continue ;;" in sh
    assert "--shipped-configure" in sh and "STAGE=deploy-unshipped" in sh
    assert deploy_parity.in_drift_scope("maint/x.py")
    assert not deploy_parity.in_drift_scope("maint/.venv/lib/x.py")
    assert not deploy_parity.in_drift_scope("data/seerr-listen.cjs")


_TOOLS = all(shutil.which(t) for t in ("bash", "md5sum", "python3"))


@pytest.mark.skipif(os.name != "posix" or not _TOOLS, reason="runs the canary body; needs posix bash")
def test_canary_body_end_to_end_unshipped_then_clean(world):
    """Run the real remote body (the text inside sshm '...') against a fixture
    box: a hand-copied configure script reads deploy-unshipped, a shipped one
    is compared, and removing the hand copy clears it."""
    repo, home, _ = world
    inst = "scripts/configure/240-maintenance-install.sh"
    (repo / "scripts/configure").mkdir(parents=True)
    (repo / inst).write_text('cp -f "$STG"/' + inst + " ~/" + inst + "\n")
    (repo / "scripts/configure/80-vlogs-install.sh").write_text("echo ws\n")
    lib = repo / "scripts/maint/lib"
    lib.mkdir(parents=True)
    shutil.copyfile(REPO / "scripts/maint/lib/deploy_parity.py", lib / "deploy_parity.py")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "box")
    _git(repo, "remote", "add", "origin", str(repo))
    dep = home / "scripts"
    (dep / "configure").mkdir(parents=True)
    shutil.copyfile(repo / inst, dep / "configure/240-maintenance-install.sh")
    shutil.copyfile(repo / "scripts/configure/80-vlogs-install.sh",
                    dep / "configure/80-vlogs-install.sh")
    sh = (REPO / "scripts/canaries/deploy-drift.sh").read_text(encoding="utf-8")
    body = sh.split("RES=$(sshm '", 1)[1].split("') || RC=$?", 1)[0]
    env = {**os.environ, "HOME": str(home), "QFLIX_SRC_DIR": str(repo),
           "QFLIX_DEPLOYED_DIR": str(dep), "QFLIX_DRIFT_REF": "master"}

    def run():
        return subprocess.run(["bash", "-c", body], env=env, capture_output=True, text=True)

    r = run()
    assert r.returncode == 1 and "STAGE=deploy-unshipped" in r.stderr, r.stderr
    assert "configure/80-vlogs-install.sh" in r.stderr
    assert "240-maintenance-install.sh" not in r.stderr
    (dep / "configure/80-vlogs-install.sh").unlink()
    r = run()
    assert r.returncode == 0, r.stderr
    assert "PASS: deploy-drift - 1 deployed files match" in r.stdout

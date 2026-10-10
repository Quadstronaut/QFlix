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
              "scripts/maint/lib/deploy_parity.py", "scripts/ops/qflix-listen-set.sh"):
        assert f + " \\" in inst, f
    assert "~/scripts/ops/qflix-listen-set.sh" in inst

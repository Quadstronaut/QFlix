"""test_ports - lib/ports.py claim(): idempotent, never hands out a claimed or
bound port, safe under concurrent claimers (QFLX-19, replaces 4 shell filters)."""
from __future__ import annotations

import multiprocessing
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "maint"))
from lib import ports  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

posix_only = pytest.mark.skipif(
    ports.fcntl is None, reason="flock/chmod semantics need POSIX (CI runs Linux)")

SS = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port
LISTEN 0 128 127.0.0.1:17001 0.0.0.0:*
LISTEN 0 128 0.0.0.0:17002 0.0.0.0:*
LISTEN 0 128 [::]:17003 [::]:*
LISTEN 0 128 *:17004 *:*
"""
APP_PORTS = "free ports:\n17001\n17002\n17003\n17004\n17005\n17006\nnot-a-port\n"


def test_parse_ss_collects_every_local_port():
    assert ports.parse_ss(SS) == {17001, 17002, 17003, 17004}


def test_parse_candidates_keeps_only_numeric_lines_in_order():
    assert ports.parse_candidates(APP_PORTS) == [17001, 17002, 17003, 17004, 17005, 17006]


def test_existing_secret_returned_without_touching_candidates(tmp_path):
    (tmp_path / "vlogs.port").write_text("17999\n")
    assert ports.claim("vlogs.port", tmp_path, [], set()) == 17999


def test_first_free_candidate_skips_bound_and_claimed(tmp_path):
    (tmp_path / "other.port").write_text("17005\n")
    got = ports.claim("vlogs.port", tmp_path, [17001, 17002, 17005, 17006], {17001, 17002})
    assert got == 17006
    assert (tmp_path / "vlogs.port").read_text().strip() == "17006"


def test_underscore_port_secret_counts_as_claimed(tmp_path):
    # tdarr.server_port is not matched by *.port; it must still be excluded.
    (tmp_path / "tdarr.server_port").write_text("17001\n")
    assert ports.claim("x.port", tmp_path, [17001, 17002], set()) == 17002


def test_idempotent(tmp_path):
    a = ports.claim("x.port", tmp_path, [17001, 17002], set())
    b = ports.claim("x.port", tmp_path, [17002, 17003], {17001})
    assert a == b == 17001


def test_no_free_port_raises_and_writes_nothing(tmp_path):
    with pytest.raises(ports.PortClaimError):
        ports.claim("x.port", tmp_path, [17001], {17001})
    assert not (tmp_path / "x.port").exists()


def test_no_partial_or_tmp_files_left(tmp_path):
    ports.claim("x.port", tmp_path, [17001], set())
    assert sorted(p.name for p in tmp_path.iterdir() if p.name not in (".ports.lock", "secrets-isolated")) == ["x.port"]


@posix_only
def test_secret_file_is_0600(tmp_path):
    ports.claim("x.port", tmp_path, [17001], set())
    assert ((tmp_path / "x.port").stat().st_mode & 0o777) == 0o600


def test_garbage_existing_secret_is_not_returned(tmp_path):
    (tmp_path / "x.port").write_text("garbage")
    assert ports.claim("x.port", tmp_path, [17001], set()) == 17001


def _worker(args):
    d, name, cands = args
    sys.path.insert(0, str(REPO / "scripts" / "maint"))
    from lib import ports as p
    return p.claim(name, Path(d), cands, set())


@posix_only
def test_concurrent_claims_never_collide(tmp_path):
    cands = list(range(17100, 17140))
    jobs = [(str(tmp_path), f"app{i}.port", cands) for i in range(16)]
    with multiprocessing.get_context("spawn").Pool(8) as pool:
        got = pool.map(_worker, jobs)
    assert len(set(got)) == 16


@posix_only
def test_lock_contention_times_out_loudly_not_with_a_collision(tmp_path, monkeypatch):
    import fcntl
    holder = open(tmp_path / ".ports.lock", "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    monkeypatch.setattr(ports, "LOCK_TIMEOUT_S", 0.3)
    try:
        with pytest.raises(ports.PortClaimError):
            ports.claim("x.port", tmp_path, [17001], set())
    finally:
        holder.close()


def test_lock_infrastructure_failure_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(ports, "_open_lock", lambda d: None)
    assert ports.claim("x.port", tmp_path, [17001], set()) == 17001


def test_cli_prints_port(tmp_path):
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts/maint/lib/ports.py"), "claim", "vlogs.port",
         "--secrets-dir", str(tmp_path), "--app-ports", APP_PORTS, "--ss", SS],
        capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "17005"


def test_cli_exhausted_exits_nonzero(tmp_path):
    r = subprocess.run(
        [sys.executable, str(REPO / "scripts/maint/lib/ports.py"), "claim", "v.port",
         "--secrets-dir", str(tmp_path), "--app-ports", "17001\n", "--ss", SS],
        capture_output=True, text=True)
    assert r.returncode != 0 and r.stdout.strip() == ""


# ---- shell wrapper: scripts/lib/ports.sh claim_port, with a fake sshm ----
def _run_sh(tmp_path, secret):
    (tmp_path / "ap.txt").write_text(APP_PORTS)
    (tmp_path / "ss.txt").write_text(SS)
    script = f"""
set -euo pipefail
die() {{ echo "DIE: $*" >&2; exit 9; }}
log_info() {{ :; }}
SECRETS_DIR='{tmp_path.as_posix()}'
sshm() {{ case "$1" in
  *app-ports*) cat '{tmp_path.as_posix()}/ap.txt' ;;
  *) cat '{tmp_path.as_posix()}/ss.txt' ;;
esac; }}
source '{(REPO / "scripts/lib/ports.sh").as_posix()}'
claim_port {secret}
cat '{tmp_path.as_posix()}/{secret}'
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_shell_claim_port_writes_secret(tmp_path):
    r = _run_sh(tmp_path, "vlogs.port")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "17005"


def test_shell_claim_port_existing_secret_unchanged_without_ssh(tmp_path):
    (tmp_path / "vlogs.port").write_text("17777\n")
    r = _run_sh(tmp_path, "vlogs.port")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "17777"


def test_no_installer_keeps_its_own_app_ports_filter():
    for f in ("240-maintenance-install.sh", "43-listmonk-install.sh",
              "50-tdarr-install.sh", "80-vlogs-install.sh"):
        text = (REPO / "scripts/configure" / f).read_text()
        assert "app-ports free" not in text, f
        assert "claim_port" in text, f

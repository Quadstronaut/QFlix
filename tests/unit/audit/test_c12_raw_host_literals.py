"""C-12 -- shrink-only ratchet on raw app-<x> calls and docker-gateway literals
(QFLX-23, spec F-16).

The gateway address now comes from the net.app_host secret and panel tools go
through ~/bin/appctl. What is left of each raw literal is baselined, per file,
in manifest/raw-host-allowlist.yaml. The allowlist may only SHRINK:
  * a literal in an unlisted file is a finding (new-literal);
  * more literals than listed is a finding (literal-grew);
  * FEWER literals than listed is also a finding (allowlist-not-shrunk), which
    forces the allowlist to be tightened the moment a literal is removed, so a
    removed literal can never be silently re-added under its old budget.
"""
from __future__ import annotations

import yaml

from lib.audit.detectors import c12_raw_host_literals as det
from lib.audit.model import FINDING, OK
from lib.audit.repo import Repo

GW = "172.17" + ".0.1"          # assembled so this test file is not itself a hit

# The ratchet's hard ceiling. Lowering is the only legal edit; raising these
# numbers is the act the ratchet exists to make loud in review.
CEILING_APP_CALLS = 28
CEILING_HOST_LITERALS = 0


def _ctx(tmp_path, files, allow=None):
    tracked = []
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        tracked.append(rel)
    if allow is not None:
        (tmp_path / det.ALLOWLIST_PATH).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / det.ALLOWLIST_PATH).write_text(
            yaml.safe_dump({"schema": 1, "files": allow}), encoding="utf-8")
        tracked.append(det.ALLOWLIST_PATH)

    class _C:
        repo = Repo(tmp_path, tracked=tracked)
        ledgers = None
    return _C()


def _kinds(result):
    return sorted((v.path, v.kind) for v in result.verdicts if v.status == FINDING)


# -- positive / negative fixtures ---------------------------------------------

def test_clean_file_is_ok(tmp_path):
    r = det.detect(_ctx(tmp_path, {"scripts/maint/a.py": "x = 1\n"}, allow={}))
    assert _kinds(r) == []


def test_new_gateway_literal_is_a_finding(tmp_path):
    body = 'URL = "http://%s:17011/"\n' % GW
    r = det.detect(_ctx(tmp_path, {"scripts/maint/new.py": body}, allow={}))
    assert _kinds(r) == [("scripts/maint/new.py", "new-literal")]


def test_new_raw_app_call_is_a_finding(tmp_path):
    body = "#!/usr/bin/env bash\napp-sonarr restart\n"
    r = det.detect(_ctx(tmp_path, {"scripts/configure/zz.sh": body}, allow={}))
    assert _kinds(r) == [("scripts/configure/zz.sh", "new-literal")]


def test_app_ports_and_nginx_are_raw_calls(tmp_path):
    body = "ap=$(app-ports free)\napp-nginx reload\n"
    r = det.detect(_ctx(tmp_path, {"scripts/lib/q.sh": body}, allow={}))
    assert _kinds(r) == [("scripts/lib/q.sh", "new-literal")]


def test_comments_and_docstrings_are_not_counted(tmp_path):
    body = ('"""Talks to %s via app-sonarr restart."""\n'
            "# app-nginx reload and %s\n"
            "x = 1\n") % (GW, GW)
    r = det.detect(_ctx(tmp_path, {"scripts/maint/doc.py": body}, allow={}))
    assert _kinds(r) == []


def test_appctl_and_variable_forms_are_not_raw_calls(tmp_path):
    body = ('"$HOME/bin/appctl" restart sonarr\n'
            'record "$app-qbit" pass\n'
            "x=--app-ports\n")
    r = det.detect(_ctx(tmp_path, {"scripts/maint/ok.sh": body}, allow={}))
    assert _kinds(r) == []


def test_policy_home_is_exempt(tmp_path):
    body = 'GW = "%s"\nsubprocess.run(["app-ports", "free"])\n' % GW
    r = det.detect(_ctx(tmp_path, {"scripts/maint/lib/hostpolicy_ultra.py": body}, allow={}))
    assert _kinds(r) == []


def test_rea_fingerprint_text_is_exempt(tmp_path):
    body = 'ECONNREFUSED %s:17025\n' % GW
    r = det.detect(_ctx(tmp_path, {"scripts/local-llm/qflix-rea.ps1": body}, allow={}))
    assert _kinds(r) == []


# -- the ratchet itself ---------------------------------------------------------

def test_allowlisted_literal_at_its_budget_is_ok(tmp_path):
    body = "app-ports free\n"
    r = det.detect(_ctx(tmp_path, {"scripts/maint/b.sh": body},
                        allow={"scripts/maint/b.sh": {"app_calls": 1}}))
    assert _kinds(r) == []
    assert any(v.status == OK and v.kind == "baselined" for v in r.verdicts)


def test_growing_a_baselined_file_is_a_finding(tmp_path):
    body = "app-ports free\napp-nginx reload\n"
    r = det.detect(_ctx(tmp_path, {"scripts/maint/b.sh": body},
                        allow={"scripts/maint/b.sh": {"app_calls": 1}}))
    assert _kinds(r) == [("scripts/maint/b.sh", "literal-grew")]


def test_removing_a_literal_without_shrinking_the_allowlist_is_a_finding(tmp_path):
    r = det.detect(_ctx(tmp_path, {"scripts/maint/b.sh": "echo hi\n"},
                        allow={"scripts/maint/b.sh": {"app_calls": 1}}))
    assert _kinds(r) == [("scripts/maint/b.sh", "allowlist-not-shrunk")]


def test_allowlist_entry_for_untracked_file_is_a_finding(tmp_path):
    r = det.detect(_ctx(tmp_path, {"scripts/maint/a.py": "x = 1\n"},
                        allow={"scripts/maint/gone.sh": {"app_calls": 2}}))
    assert _kinds(r) == [("scripts/maint/gone.sh", "allowlist-not-shrunk")]


def test_gateway_and_app_budgets_are_independent(tmp_path):
    body = 'H="%s"\napp-ports free\n' % GW
    r = det.detect(_ctx(tmp_path, {"scripts/maint/b.sh": body},
                        allow={"scripts/maint/b.sh": {"app_calls": 1}}))
    assert _kinds(r) == [("scripts/maint/b.sh", "literal-grew")]


# -- the real repo ----------------------------------------------------------------

def test_real_repo_is_clean_against_its_allowlist(ctx):
    r = det.detect(ctx)
    bad = [(v.path, v.kind) for v in r.verdicts if v.status == FINDING]
    assert bad == []


def test_real_allowlist_total_never_exceeds_the_ceiling(repo):
    data = yaml.safe_load(repo.read(det.ALLOWLIST_PATH))
    files = data["files"]
    app = sum(int(e.get("app_calls", 0)) for e in files.values())
    host = sum(int(e.get("host_literals", 0)) for e in files.values())
    assert app <= CEILING_APP_CALLS, "allowlist grew: app calls %d" % app
    assert host <= CEILING_HOST_LITERALS, "allowlist grew: host literals %d" % host


def test_converted_files_carry_no_gateway_literal(repo):
    """The QFLX-23 conversion list: each of these now reads net.app_host."""
    converted = [
        "scripts/configure/03-prowlarr-flaresolverr.sh",
        "scripts/configure/09-phase5-arr-connects-and-sync.py",
        "scripts/configure/09-phase5-arr-connects-and-sync.sh",
        "scripts/configure/30-seerr-arrs.py",
        "scripts/configure/50-tautulli-pms-url-fix.sh",
        "scripts/configure/90-sabnzbd-usenet-install.sh",
        "scripts/configure/90b-usenet-all-arrs.py",
        "scripts/data/unpackerr.conf.tmpl",
        "scripts/install/02-flaresolverr.sh",
        "scripts/maint/flaresolverr-canary.py",
        "scripts/maint/flaresolverr-unsuppress-watch.sh",
        "scripts/maint/functional-audit.py",
        "scripts/ops/tautulli-gate-watch.sh",
        "scripts/smoke/arr-audit-fixes.py",
        "manifest/apps.yaml",
    ]
    for rel in converted:
        assert GW not in "\n".join(det.code_lines(rel, repo.read(rel))), rel

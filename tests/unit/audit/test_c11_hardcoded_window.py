"""C-11 -- no new hard-coded Monday maintenance-window literal (QFLX-16, O-5)."""
from __future__ import annotations

from lib.audit.detectors import c11_hardcoded_window as det
from lib.audit.model import FINDING, OK
from lib.audit.repo import Repo


def _ctx(tmp_path, rel, body):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")

    class _C:
        repo = Repo(tmp_path, tracked=[rel])
        ledgers = None
    return _C()


def test_real_repo_has_no_unadjudicated_monday_literal(ctx):
    result = det.detect(ctx)
    bad = [v for v in result.verdicts if v.status == FINDING]
    assert bad == [], [(v.path, v.line) for v in bad]


def test_new_weekday_literal_is_a_finding(tmp_path):
    body = "def f(now):\n    return now.weekday() == 0 and 11 <= now.hour < 15\n"
    r = det.detect(_ctx(tmp_path, "scripts/maint/qflix-new.py", body))
    assert [v.kind for v in r.verdicts] == ["hardcoded-monday-window"]
    assert r.verdicts[0].status == FINDING


def test_shell_date_u_literal_is_a_finding(tmp_path):
    body = '#!/usr/bin/env bash\nDOW=$(date -u +%u)\n'
    r = det.detect(_ctx(tmp_path, "scripts/canaries/new.sh", body))
    assert r.verdicts[0].status == FINDING


def test_window_ok_marker_adjudicates_a_cadence(tmp_path):
    body = ("x = now.isoweekday() == 1   # window-ok: weekly send cadence\n"
            "# window-ok: digest day\n"
            "y = now.weekday() == 0\n")
    r = det.detect(_ctx(tmp_path, "scripts/maint/qflix-cadence.py", body))
    assert len(r.verdicts) == 2
    assert all(v.status == OK and v.kind == "adjudicated-cadence" for v in r.verdicts)


def test_policy_module_is_the_legit_home(tmp_path):
    body = "def w(now):\n    return now.weekday() == 0\n"
    r = det.detect(_ctx(tmp_path, "scripts/maint/lib/hostpolicy_ultra.py", body))
    assert r.verdicts[0].status == OK and r.verdicts[0].kind == "policy-module"


def test_comments_are_not_counted(tmp_path):
    body = "# the old check was now.weekday() == 0\nz = 1\n"
    r = det.detect(_ctx(tmp_path, "scripts/maint/qflix-c.py", body))
    assert r.boundary_size == 0

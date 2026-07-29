"""C-09 — a missing prerequisite must never produce a clean exit.

Memory kuma-monitors-born-mute: "a missing token silently exits 0". Kuma sees a
green push it never received and the check has been dead for weeks.
"""
from __future__ import annotations

import re

from lib.audit.detectors import c09_silent_exit as det
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


def test_enumerates_the_canary_exit_sites(ctx, repo):
    """The boundary is re-derived: every non-comment `exit 0` in the canaries.
    The first cut of this detector used `^` without leading whitespace and saw
    16 of 30 — under-counting your own boundary is the original defect."""
    result = det.detect(ctx)
    ref = 0
    for path in repo.tracked_matching(["scripts/canaries/*.sh"]):
        for line in repo.read(path).split("\n"):
            if line.strip().startswith("#"):
                continue
            # Several canaries embed a Python heredoc, so `sys.exit(0)` is a
            # clean-exit site inside a .sh file too. A shell-only reference
            # would have quietly shrunk the boundary by 7.
            if re.search(r"\b(?:exit|return)\s+0\b|sys\.exit\(0\)", line):
                ref += 1
    assert ref > 20, "reference count looks wrong: " + str(ref)
    canary_sites = [v for v in result.verdicts if v.path.startswith("scripts/canaries/")]
    assert len(canary_sites) == ref
    assert result.boundary_size == len(result.verdicts)


def test_zero_findings_is_auditable_not_merely_reassuring(ctx):
    """C-09 currently reports zero on the real repo. That could mean the
    born-mute class really was fixed (2026-07-27) — or that the detector's
    first filter never fires and it is reporting zero about nothing. The
    sub-counts distinguish the two, so nobody has to take it on faith."""
    m = det.detect(ctx).metrics
    assert m["clean_exit_sites"] > 50
    assert m["sites_behind_a_guard"] > 0, (
        "no clean exit sits behind ANY missing-prerequisite guard — the "
        "detector is almost certainly mis-scoped, not the repo clean")
    assert m["sites_declared_skip"] <= m["sites_behind_a_guard"]


def test_missing_token_guard_is_a_finding(tmp_path):
    body = (
        "#!/usr/bin/env bash\n"
        "TOKEN=$(cat ~/secrets/kuma-push-tokens.json | jq -r .foo)\n"
        'if [ -z "$TOKEN" ]; then\n'
        "  exit 0\n"
        "fi\n"
    )
    result = det.detect(_ctx(tmp_path, "scripts/canaries/x.sh", body))
    findings = [v for v in result.verdicts if v.status == FINDING]
    assert len(findings) == 1
    assert findings[0].kind == "guard-exits-clean"


def test_declared_skip_is_not_a_finding(tmp_path):
    """Skipping inside the Monday maintenance window is a decision, not a
    missing prerequisite."""
    body = (
        "#!/usr/bin/env bash\n"
        'if [ ! -f "$STATE/window.lock" ] && in_pause_window; then\n'
        "  echo 'maintenance window - skipping by design'\n"
        "  exit 0\n"
        "fi\n"
    )
    result = det.detect(_ctx(tmp_path, "scripts/canaries/y.sh", body))
    assert [v for v in result.verdicts if v.status == FINDING] == []


def test_successful_path_exit_is_not_a_finding(tmp_path):
    body = "#!/usr/bin/env bash\necho PASS\nexit 0\n"
    result = det.detect(_ctx(tmp_path, "scripts/canaries/z.sh", body))
    v = result.verdicts[0]
    assert v.status == OK and v.kind == "exit-not-a-guard"


def test_python_sys_exit_zero_is_in_boundary(tmp_path):
    body = (
        "import os\n"
        "def main():\n"
        "    if not os.path.exists(TOKEN_PATH):\n"
        "        sys.exit(0)\n"
    )
    result = det.detect(_ctx(tmp_path, "scripts/maint/j.py", body))
    assert len(result.verdicts) == 1
    assert result.verdicts[0].status == FINDING


def test_comment_lines_are_not_counted(tmp_path):
    body = "#!/usr/bin/env bash\n# no token? then exit 0 quietly\necho hi\n"
    result = det.detect(_ctx(tmp_path, "scripts/canaries/c.sh", body))
    assert result.boundary_size == 0

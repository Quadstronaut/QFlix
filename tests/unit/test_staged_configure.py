"""One list, two consumers — configure-script staging cannot diverge.

WHY THIS FILE EXISTS
--------------------
`240-maintenance-install.sh` used to express its configure-script allowlist
TWICE: once as a path in the `tar` file list and once as a `cp -f` line in the
remote staging heredoc. `60-www-images.sh` was in NEITHER, while a copy of it
had been resident under `~/scripts/configure/` since some past manual scp. So:

  * `deploy-drift` was GREEN on it, because the deployed bytes happened to match
    origin/master (nobody had edited it since the scp);
  * PRs #32 / #34 / #36 / #37 then edited it — the FAQ deploy and its smoke
    checks — and every one of them merged, went green in CI, and **never reached
    the box**;
  * running the installer could not fix it, because the installer did not know
    the file existed.

That is the same class the 240 comment block already described for ITSELF on
2026-09-12: *a file resident under ~/scripts with no stager is unfixable by
running the installer*. The lesson was written down for one file and not
generalised, so it recurred one file later.

TWO HALVES, DELIBERATELY
  1. this file — the SOURCE invariant: there is exactly one declared list and
     both consumers are generated from it, so divergence is structurally
     impossible rather than merely currently-absent.
  2. `deploy-drift.sh`'s `unstaged-deployed-file` stage (behaviourally executed
     in tests/unit/test_deploy_drift_exit_contract.py) — the BOX invariant:
     any *.sh deployed under ~/scripts/configure/ that the box-resident
     installer does not name is a finding, BEFORE anyone edits it.

Neither half alone is sufficient: (1) cannot see a file that was hand-scp'd, and
(2) cannot see a file that is in git but deployed nowhere yet.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "configure" / "240-maintenance-install.sh"
TEXT = INSTALLER.read_text(encoding="utf-8")

# Every configure script that MUST be staged, with why. Removing a name from
# STAGED_CONFIGURE silently un-deploys a file; this is the assertion that makes
# that mutation fail loudly instead.
REQUIRED: dict[str, str] = {
    "55-kometa-install.sh":
        "the kometa-deploy-drift canary parses its heredoc for the library names",
    "240-maintenance-install.sh":
        "resident since a past hand-scp; deploy-drift walks EVERY *.sh under ~/scripts, "
        "so an unstaged copy of the installer can never be brought into agreement "
        "by running the installer (2026-09-12)",
    "60-www-images.sh":
        "FAQ deploy + smoke checks; PRs #32/#34/#36/#37 merged and never reached "
        "the box because nothing staged it (2026-09-17)",
}


def staged_configure() -> list[str]:
    """Parse the declared array — the same way deploy-drift.sh does on the box,
    so a change that breaks one parser is very likely to break the other."""
    m = re.search(r"^STAGED_CONFIGURE=\(\s*$(.*?)^\)\s*$", TEXT, re.S | re.M)
    assert m, "STAGED_CONFIGURE array not found in 240-maintenance-install.sh"
    names: list[str] = []
    for line in m.group(1).splitlines():
        line = line.split("#", 1)[0].strip()
        names.extend(line.split())
    return names


def test_the_array_exists_and_holds_every_required_script():
    names = staged_configure()
    assert names, "STAGED_CONFIGURE parsed empty"
    for want, why in REQUIRED.items():
        assert want in names, f"{want} must be staged: {why}"


def test_this_instance_is_fixed():
    """AC-9, stated on its own so the regression has a name."""
    assert "60-www-images.sh" in staged_configure()


def test_every_staged_name_is_a_real_bare_basename():
    """The array is splatted unquoted into the remote prologue
    (`printf 'STAGED_CONFIGURE=(%s)' "${STAGED_CONFIGURE[*]}"`), which is only
    safe while every member is a bare, whitespace-free basename that exists."""
    for name in staged_configure():
        assert "/" not in name, name
        assert re.fullmatch(r"[A-Za-z0-9._-]+\.sh", name), name
        assert (REPO / "scripts" / "configure" / name).is_file(), name


def test_the_tar_consumer_reads_the_array_and_nothing_else():
    """AC-10, direction 1. No literal `scripts/configure/<x>.sh` may appear in
    the tar file list — if it could, it could be there without a matching cp."""
    tar_block = TEXT.split('( cd "$REPO_ROOT" && tar -cf - \\', 1)[1]
    tar_block = tar_block.split("| sshm", 1)[0]
    assert '"${STAGED_CONFIGURE[@]/#/scripts/configure/}"' in tar_block
    literals = re.findall(r"scripts/configure/[A-Za-z0-9._-]+\.sh", tar_block)
    assert literals == [], f"hand-written configure paths in the tar list: {literals}"


def test_the_cp_consumer_reads_the_array_and_nothing_else():
    """AC-10, direction 2. Same for the remote staging step."""
    stage_block = TEXT.split("cat <<'STAGE'", 1)[1].split("\nSTAGE\n", 1)[0]
    assert 'for _cf in "${STAGED_CONFIGURE[@]}"; do' in stage_block
    literals = re.findall(r'cp -f\s+"\$STG"/scripts/configure/[A-Za-z0-9._-]+\.sh',
                          stage_block)
    assert literals == [], f"hand-written configure cp lines: {literals}"


def test_the_array_is_shipped_to_the_remote_side():
    """The prologue is what makes the heredoc (which is quoted, so nothing local
    expands inside it) able to read the same list. Without this line the cp loop
    would iterate an unset array under `set -u` and the install would die."""
    assert "printf 'STAGED_CONFIGURE=(%s)\\n' \"${STAGED_CONFIGURE[*]}\"" in TEXT
    assert "} | sshm 'bash -s'" in TEXT


def test_there_is_exactly_one_declaration():
    assert len(re.findall(r"^STAGED_CONFIGURE=\(", TEXT, re.M)) == 1


def test_both_consumers_resolve_to_the_identical_set():
    """The property the two tests above buy, stated as the property: whatever
    the array holds, both consumers stage exactly that, with no residue."""
    names = set(staged_configure())
    tar_block = TEXT.split('( cd "$REPO_ROOT" && tar -cf - \\', 1)[1].split("| sshm", 1)[0]
    stage_block = TEXT.split("cat <<'STAGE'", 1)[1].split("\nSTAGE\n", 1)[0]
    tar_names = set(re.findall(r"scripts/configure/([A-Za-z0-9._-]+\.sh)", tar_block))
    cp_names = set(re.findall(r'/scripts/configure/([A-Za-z0-9._-]+\.sh)', stage_block))
    # Both are EMPTY of literals — the only names either consumer can produce
    # come from the array at expansion time.
    assert tar_names == set()
    assert cp_names == set()
    assert names == set(staged_configure())

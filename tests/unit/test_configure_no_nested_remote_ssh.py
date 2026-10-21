"""QFLX-61: no local SSH helper may run inside a remote heredoc.

`sshm "bash -s" <<'X' ... X` ships the heredoc body to the BOX and runs it
there. `sshm`, `scpm_to` and `scpm_from` are local functions sourced from
scripts/lib/ssh.sh; on the box they do not exist. 50-tdarr-install.sh nested a
second `sshm "bash -s" <<'SHIMSCRIPT'` inside its Step 6 UNITSCRIPT body, so
the box hit `sshm: command not found` (127) under `set -e`: the node unit was
never rewritten, nothing reloaded or restarted, and the installer died at
step 6. Plain `ssh`/`scp` from the box is just as wrong — the slot holds no
key back to itself or onward.

The guard is generic (every scripts/configure/*.sh), because the shape is
invisible in review: the inner call reads exactly like the outer one.
"""
from __future__ import annotations

import glob
import os
import re

_REPO = os.path.join(os.path.dirname(__file__), "..", "..")
_CONFIGURE = os.path.join(_REPO, "scripts", "configure")
_TDARR = os.path.join(_CONFIGURE, "50-tdarr-install.sh")

# A line that hands a heredoc to a remote-exec helper. Captures the `-` of
# `<<-` (tab-stripped terminator) and the delimiter word, quoted or not.
_REMOTE_HEREDOC = re.compile(
    r"""(?:^|[\s;&|($])(?:sshm|ssh)(?![\w-])[^#\n]*?<<(-?)\s*['"]?([A-Za-z_][A-Za-z0-9_]*)['"]?"""
)
# A helper invoked in COMMAND position: line start, after a separator, or at
# the start of a command substitution. Prose that merely mentions "ssh" is
# not a call, and neither is a different tool that shares the prefix
# (`ssh-keygen` on the box is legitimate), hence the (?![\w-]) tail.
_LOCAL_CALL = re.compile(
    r"(?:^|[;&|]\s*|\$\(\s*|\b(?:then|do|else)\s+)(sshm|ssh|scpm_to|scpm_from|scp)(?![\w-])"
)


def remote_heredoc_violations(text: str) -> list[tuple[int, str, str]]:
    """Return (line_no, delimiter, offending line) for each nested call."""
    lines = text.splitlines()
    out: list[tuple[int, str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()
        m = None if stripped.startswith("#") else _REMOTE_HEREDOC.search(line)
        if not m:
            i += 1
            continue
        strip_tabs, delim = m.group(1) == "-", m.group(2)
        j = i + 1
        while j < len(lines):
            body = lines[j].lstrip("\t") if strip_tabs else lines[j]
            if body == delim:
                break
            code = lines[j].strip()
            if code and not code.startswith("#") and _LOCAL_CALL.search(code):
                out.append((j + 1, delim, code))
            j += 1
        # Resume AFTER the terminator: the body is opaque to the local shell,
        # so a heredoc opened inside it is part of the remote script (and
        # already reported above), not a new top-level remote call.
        i = j + 1
    return out


def _configure_scripts() -> list[str]:
    return sorted(glob.glob(os.path.join(_CONFIGURE, "*.sh")))


def test_no_ssh_helper_inside_any_remote_heredoc():
    bad = []
    for p in _configure_scripts():
        with open(p, encoding="utf-8") as fh:
            for ln, delim, code in remote_heredoc_violations(fh.read()):
                bad.append(f"{os.path.basename(p)}:{ln} (inside <<{delim}): {code}")
    assert not bad, (
        "local ssh helper called inside a remote heredoc body — it runs ON "
        "THE BOX, where sshm/scpm_* do not exist. Split it into sequential "
        "top-level calls:\n  " + "\n  ".join(bad)
    )


def test_guard_catches_the_qflx61_shape():
    # The exact shape that shipped broken. If the parser cannot see this, the
    # guard above is vacuous.
    broken = (
        "sshm \"bash -s\" <<'UNITSCRIPT'\n"
        "set -euo pipefail\n"
        "cat > x <<'UNIT'\n[Unit]\nUNIT\n"
        "sshm \"bash -s\" <<'SHIMSCRIPT'\n"
        "echo hi\n"
        "SHIMSCRIPT\n"
        "systemctl --user daemon-reload\n"
        "UNITSCRIPT\n"
    )
    hits = remote_heredoc_violations(broken)
    assert [(d, c.split()[0]) for _, d, c in hits] == [("UNITSCRIPT", "sshm")]


def test_guard_ignores_sequential_top_level_calls_and_prose():
    ok = (
        "sshm \"bash -s\" <<'A'\n"
        "# a comment that says sshm and ssh\n"
        "echo 'run sshm locally'\n"
        "A\n"
        "sshm \"bash -s\" <<'B'\n"
        "echo b\n"
        "B\n"
        "scpm_to a b\n"
    )
    assert remote_heredoc_violations(ok) == []


def test_guard_sees_tab_stripped_and_command_substitution_forms():
    text = "X=$(sshm 'bash -s' <<-EOF\n\tY=$(sshm true)\n\tEOF\n)\n"
    assert [d for _, d, _ in remote_heredoc_violations(text)] == ["EOF"]


def test_guard_allows_box_side_tools_sharing_the_prefix():
    text = "sshm 'bash -s' <<'K'\nssh-keygen -l -f x\nX=$(ssh-keygen -l)\nK\n"
    assert remote_heredoc_violations(text) == []


def _tdarr_src() -> str:
    with open(_TDARR, encoding="utf-8") as fh:
        return fh.read()


def test_tdarr_installer_ships_the_shim_before_installing_it():
    # The shim install copies from ~/scripts/ops/ffmpeg-threadcap-shim.sh; no
    # installer shipped it, so the only box copy was a hand copy. Ship first.
    src = _tdarr_src()
    ship = src.find("scpm_to \"$HERE/../ops/ffmpeg-threadcap-shim.sh\"")
    use = src.find('scripts/ops/ffmpeg-threadcap-shim.sh" "$D/')
    assert ship != -1, "50-tdarr-install.sh must scpm_to the threadcap shim"
    assert use != -1, "shim install step not found"
    assert ship < use, "shim must be shipped BEFORE the step that copies it"


def test_tdarr_installer_ships_the_port_drain_before_the_unit_uses_it():
    # Same gap: ExecStartPre has no `-` prefix, so a missing drain script
    # refuses every server start.
    src = _tdarr_src()
    ship = src.find("scpm_to \"$HERE/../ops/tdarr-port-drain.sh\"")
    use = src.find("ExecStartPre=%h/scripts/ops/tdarr-port-drain.sh")
    assert ship != -1 and use != -1
    assert ship < use


def test_tdarr_shim_installed_in_both_bundles_and_self_tested():
    src = _tdarr_src()
    assert "Tdarr_Node/node_modules/ffmpeg-static" in src
    assert "Tdarr_Server/node_modules/ffmpeg-static" in src
    # Real binary renamed aside exactly once; the shim execs ffmpeg.real.
    assert 'mv "$D/ffmpeg" "$D/ffmpeg.real"' in src
    assert '[ ! -e "$D/ffmpeg.real" ]' in src
    # -version must pass straight through, or Tdarr's start-up binary test
    # bricks the node.
    assert "ffmpeg -version" in src.replace('/ffmpeg" -version', "/ffmpeg -version")


def test_tdarr_units_reloaded_once_after_both_are_written():
    src = _tdarr_src()
    assert src.count("systemctl --user daemon-reload") == 1
    reload_at = src.index("systemctl --user daemon-reload")
    assert src.index("tdarr-server.service <<'UNIT'") < reload_at
    assert src.index("tdarr-node.service <<'UNIT'") < reload_at
    assert reload_at < src.index("systemctl --user enable tdarr-server.service tdarr-node.service")

"""REA collector freshness, executed rather than asserted about.

WHY THIS FILE EXISTS
--------------------
On 2026-08-25 REA paged the operator with "tdarr:permission-denied", quoting

    EACCES: permission denied, mkdir
    '/tdarr-workDir-node-baY4PcyP1-worker-gloomy-goa-ts-1779348004454'

That `ts-` value is an epoch in milliseconds: 2026-05-21T07:20:04Z. THREE MONTHS
old. It shipped because ~/.apps/tdarr/logs/node.err carries ZERO dated lines
(0 of 694, measured on the box), so every FRESH_CUTOFF line filter is a provable
no-op on it, and the only surviving gate was `find -mtime`, which grades the
FILE. node.err's mtime was 1.4 days old while its tail-80 window spanned 94 days.
No noise class can fix that - the sixth rule for one unfilterable file would not
have converged either - so the collector had to stop shipping the bytes.

The fix is a DECLARED FRESHNESS BASIS per source, enforced in the collector:

    line       every shipped line carries its own date, compared to FRESH_CUTOFF
    watermark  undated append-only stream: only bytes appended since the previous
               run ship, floored by the file mtime gate (`tailnew`)
    query      the collector asks for a bounded window at query time

and a section that declares NONE ships a `# collector-error:` marker plus zero
content, never a silent pass.

WHY IT RUNS THE REAL BASH
-------------------------
Every prior REA regression was a string assertion about the heredoc, and string
assertions cannot tell a filter that runs from a filter that is a no-op - which
is exactly how `freshlines` sat "covering" three files it could never affect.
This file extracts the heredoc `Get-RemoteHeredoc` actually ships, fills the same
template holes the PowerShell side fills, and runs it against a synthetic $HOME
with real files, real mtimes and real `find`/`awk`/`base64`. The assertions are
on the decoded blob the models would have been handed.

Requires GIT BASH specifically (see _resolve_git_bash - a bare `bash` on this
workstation is WSL, which would make every assertion vacuously green) and the
gitignored qflix-rea.ps1. Skips cleanly when either is absent.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
REA_PS1 = REPO_ROOT / "scripts" / "local-llm" / "qflix-rea.ps1"

SECTION_CAP = 3000
FRESH_DAYS = 3
DAY = 86400


def _resolve_git_bash() -> str | None:
    """GIT BASH EXPLICITLY, never `shutil.which("bash")`.

    On this workstation a bare `bash` resolves to C:\\Windows\\System32\\bash.exe,
    which is WSL: a different filesystem namespace with its own /home/<user>. It
    ignores the $HOME this test hands it, so every collector reads the real
    seedbox-shaped paths under the WSL home, finds nothing, and every assertion
    "passes" against an empty blob - a vacuous green. The heredoc also targets
    the box's Git-Bash-compatible toolchain, so Git Bash is the right runtime as
    well as the only working one. Same resolver the PowerShell suite uses for
    its `bash -n` gate, and for the same reason.
    """
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / "scoop/apps/git/current/bin/bash.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Git/bin/bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Git/bin/bash.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    # Non-Windows CI: a bare bash is not WSL there.
    if os.name != "nt":
        return shutil.which("bash")
    return None


BASH = _resolve_git_bash()

# qflix-rea.ps1 is GITIGNORED (.gitignore:61) - REA runs on the workstation, not
# on the box, and the script is carried by backup-untracked.ps1 rather than by
# git. So this file is a workstation gate, not a CI gate, and must skip rather
# than fail anywhere the artifact does not exist.
pytestmark = pytest.mark.skipif(
    BASH is None or not REA_PS1.exists(),
    reason="needs Git Bash and the (untracked) qflix-rea.ps1",
)

# The 2026-08-25 false positive, verbatim, as node.err actually holds it.
STALE_EACCES = (
    "Error: EACCES: permission denied, mkdir "
    "'/tdarr-workDir-node-baY4PcyP1-worker-gloomy-goa-ts-1779348004454'\n"
    "  errno: -13,\n"
    "  syscall: 'mkdir',\n"
    "  code: 'EACCES',\n"
)


# --------------------------------------------------------------------------- #
# Extract the artifact under test
# --------------------------------------------------------------------------- #
def extract_heredoc() -> str:
    """The bash Get-RemoteHeredoc ships, with the template holes filled.

    Mirrors the PowerShell side exactly: the same two -replace calls, the same
    empty heartbeat hole (an empty hole makes the box write nothing), and the
    same CRLF strip. Anything else here would be testing a different program.
    """
    text = REA_PS1.read_text(encoding="utf-8").replace("\r\n", "\n")
    start = text.index("$bash = @'\n") + len("$bash = @'\n")
    end = text.index("\n'@", start)
    bash = text[start:end]
    bash = bash.replace("__SECTION_CAP__", str(SECTION_CAP))
    bash = bash.replace("__FRESH_DAYS__", str(FRESH_DAYS))
    bash = bash.replace("__REA_HEARTBEAT_B64__", "")
    return bash


def _posix(p: Path) -> str:
    """Windows path -> the /c/... form Git Bash uses for $HOME and for the
    per-line [path] prefixes the collector emits."""
    w = str(p).replace("\\", "/")
    if os.name == "nt" and len(w) > 2 and w[1] == ":":
        return "/" + w[0].lower() + w[2:]
    return w


def run_collector(home: Path, bash_text: str | None = None) -> dict[str, str]:
    """Run the collector against a synthetic $HOME; return decoded sections.

    Sources that need journalctl / sqlite3 / curl are absent here and every
    collector is `|| true` guarded, so they contribute whatever their error text
    is - irrelevant to the freshness question and deliberately not asserted on.
    """
    script = bash_text if bash_text is not None else extract_heredoc()
    env = dict(os.environ)
    env["HOME"] = _posix(home)
    proc = subprocess.run(
        [BASH, "-s"],
        input=script.encode("utf-8"),
        capture_output=True,
        cwd=str(home),
        env=env,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-2000:]
    blob = json.loads(proc.stdout.decode("utf-8", "replace"))
    return {
        k: base64.b64decode(v).decode("utf-8", "replace") if v else ""
        for k, v in blob["sources"].items()
    }


def write(path: Path, content: str, age_days: float = 0.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    if age_days:
        stamp = time.time() - age_days * DAY
        os.utime(path, (stamp, stamp))


def seed_watermark(home: Path, *files: Path) -> None:
    """Pretend a previous run already watched these files, at their current size.

    Equivalent to running the collector once, minus a ~5s Git Bash round trip.
    `test_the_2026_08_25_page_cannot_recur` deliberately does NOT use this - it
    proves the real first-sight behaviour end to end.
    """
    off = home / ".opt" / "maint" / "rea" / "offsets"
    off.parent.mkdir(parents=True, exist_ok=True)
    # Three fields: path, size, MARK TIME. The mark carries its own age because
    # a byte delta is not a time bound -- it says the bytes are new SINCE THE
    # MARK and nothing about how old the mark is. See the stale-mark guard in
    # tailnew.
    now = int(time.time())
    off.write_text(
        "".join(f"{_posix(f)} {f.stat().st_size} {now}\n" for f in files),
        encoding="utf-8",
    )


def read_offsets(home: Path) -> dict[str, int]:
    f = home / ".opt" / "maint" / "rea" / "offsets"
    if not f.exists():
        return {}
    out = {}
    for line in f.read_text(encoding="utf-8").splitlines():
        parts = line.rsplit(" ", 2)
        if len(parts) == 3:
            out[parts[0]] = int(parts[1])
    return out


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".apps" / "tdarr" / "logs").mkdir(parents=True)
    (h / ".apps" / "kometa" / "logs").mkdir(parents=True)
    return h


def node_err(home: Path) -> Path:
    return home / ".apps" / "tdarr" / "logs" / "node.err"


# --------------------------------------------------------------------------- #
# THE CRITICAL CASE
# --------------------------------------------------------------------------- #
def test_undated_source_older_than_the_window_yields_zero_bytes(home: Path) -> None:
    """A source with no line timestamps whose mtime predates the window ships
    NOTHING - not a filtered subset, nothing.

    The watermark is pre-seeded BELOW the file size, so the file has "grown"
    and every watermark test would pass it. The mtime floor is the thing under
    test here: it is the fallback basis for a source that cannot be line-dated,
    and it must be able to zero the section on its own.
    """
    write(node_err(home), STALE_EACCES * 4, age_days=FRESH_DAYS + 7)
    off = home / ".opt" / "maint" / "rea" / "offsets"
    off.parent.mkdir(parents=True, exist_ok=True)
    off.write_text(
        f"{_posix(home)}/.apps/tdarr/logs/node.err 10 {int(time.time())}\n",
        encoding="utf-8")

    sections = run_collector(home)

    assert "node.err" not in sections["tdarr"]
    assert "EACCES" not in sections["tdarr"]
    assert "1779348004454" not in sections["tdarr"]
    assert sections["tdarr"].strip() == ""


def test_the_2026_08_25_page_cannot_recur(home: Path) -> None:
    """End to end, on the real bytes: the three-month-old EACCES line is
    unreachable, and a genuinely new one still gets through.

    Run 1 is first sight - the collector records the watermark and ships
    nothing, because bytes that were already there when we started watching
    cannot be proven recent. Run 2 sees only what was appended in between.
    """
    write(node_err(home), STALE_EACCES)
    first = run_collector(home)
    assert first["tdarr"].strip() == "", "first sight must ship nothing"
    assert read_offsets(home), "first sight must still record the watermark"

    with node_err(home).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("Error: EACCES: permission denied, mkdir '/tdarr-workDir-ts-9999'\n")

    second = run_collector(home)
    assert "ts-9999" in second["tdarr"], "a genuinely new fault must still ship"
    assert "1779348004454" not in second["tdarr"], "the May-21 line is unreachable"
    assert "gloomy-goa" not in second["tdarr"]


def test_unchanged_undated_file_ships_nothing(home: Path) -> None:
    """The steady state. An .err file nobody wrote to contributes zero bytes,
    which is what stops a static stack trace re-paging every hour for months.
    """
    write(node_err(home), STALE_EACCES)
    seed_watermark(home, node_err(home))
    assert run_collector(home)["tdarr"].strip() == ""
    assert run_collector(home)["tdarr"].strip() == ""


def test_appended_bytes_ship_with_collector_issued_path_provenance(home: Path) -> None:
    """Only the delta ships, and every line of it carries its own [path].

    Per-line prefix rather than a header is the whole point: a byte cut can
    orphan a line from a header, it can never orphan a line from its own
    prefix. This is the collector metadata Resolve-FindingFile joins on.
    """
    write(node_err(home), "old line one\nold line two\n")
    seed_watermark(home, node_err(home))
    with node_err(home).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("TypeError: brand new failure\n")

    tdarr = run_collector(home)["tdarr"]
    assert "brand new failure" in tdarr
    assert "old line one" not in tdarr
    for line in tdarr.splitlines():
        if "brand new failure" in line:
            assert line.startswith(f"[{_posix(home)}/.apps/tdarr/logs/node.err] ")
            break
    else:
        pytest.fail("appended line was not [path]-prefixed")


def test_rotation_resets_the_watermark(home: Path) -> None:
    """A shrinking file was rotated or truncated; everything in it is then new
    by construction, so the watermark must not stay parked past end-of-file and
    blind the source for ever.
    """
    write(node_err(home), "x" * 4000 + "\n")
    seed_watermark(home, node_err(home))
    write(node_err(home), "post-rotation fault line appears here\n")

    tdarr = run_collector(home)["tdarr"]
    assert "post-rotation fault line appears here" in tdarr


def test_watermark_advances_even_when_nothing_is_reported(home: Path) -> None:
    """The mark moves on every run. If it only advanced when we shipped, one
    stale burst would re-ship for ever - the defect wearing a new hat.
    """
    write(node_err(home), STALE_EACCES, age_days=FRESH_DAYS + 7)
    run_collector(home)
    marks = read_offsets(home)
    key = f"{_posix(home)}/.apps/tdarr/logs/node.err"
    assert marks.get(key) == node_err(home).stat().st_size


def test_kometa_is_on_the_same_basis(home: Path) -> None:
    """kometa.err measured 0 dated lines in 138,689. It gets the same treatment;
    a fix wired into one of the two undated sources is a half fix.
    """
    k = home / ".apps" / "kometa" / "logs" / "kometa.err"
    write(k, "| ancient table row |\n" * 50)
    assert run_collector(home)["kometa"].strip() == ""
    with k.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("Traceback: kometa exploded just now\n")
    assert "kometa exploded just now" in run_collector(home)["kometa"]


# --------------------------------------------------------------------------- #
# The declaration itself
# --------------------------------------------------------------------------- #
def test_every_collected_section_declares_a_freshness_basis() -> None:
    """The assembly list and the declaration table must not drift. A section in
    one and not the other is how a source silently inherits "unbounded".
    """
    bash = extract_heredoc()
    listed = re.search(r"^for k in ((?:\w+ )+\w+); do$", bash, re.M)
    assert listed, "could not find the section assembly list"
    sections = listed.group(1).split()
    assert len(sections) == 14

    body = re.search(r"src_basis\(\) \{(.*?)\n\}", bash, re.S)
    assert body, "src_basis table missing"
    declared: dict[str, str] = {}
    for arm in re.finditer(r"^\s*([\w|]+)\)\s+echo (\S+) ;;", body.group(1), re.M):
        for name in arm.group(1).split("|"):
            declared[name] = arm.group(2)

    missing = [s for s in sections if s not in declared]
    assert not missing, f"sections with no declared freshness basis: {missing}"
    legal = {"line", "watermark", "query", "line+watermark"}
    assert set(declared.values()) <= legal, declared
    # `mtime` must never become a legal declaration. It is the gate that failed:
    # it bounds the FILE, not its content.
    assert "mtime" not in declared.values()
    assert declared["tdarr"] == "line+watermark"
    assert declared["kometa"] == "watermark"


def test_an_undeclared_section_withholds_content_and_says_so(home: Path) -> None:
    """MUTATION PROOF. Strip kometa's declaration and the section must become a
    NAMED configuration error carrying zero content - never a silent pass.

    Without this the declaration is decoration: a 15th source added without an
    entry would fall through to whatever the default happened to be.
    """
    k = home / ".apps" / "kometa" / "logs" / "kometa.err"
    write(k, "| ancient table row |\n" * 50)
    seed_watermark(home, k)
    with k.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("Traceback: kometa exploded just now\n")

    mutated = extract_heredoc().replace(
        "kometa)  echo watermark ;;", 'kometa)  echo "" ;;'
    )
    assert 'kometa)  echo "" ;;' in mutated, "mutation did not apply"

    sections = run_collector(home, mutated)
    assert "# collector-error:" in sections["kometa"]
    assert "section=kometa" in sections["kometa"]
    assert "kometa exploded just now" not in sections["kometa"]


# --------------------------------------------------------------------------- #
# Line-safe truncation
# --------------------------------------------------------------------------- #
def test_the_byte_cap_never_cuts_a_line_in_half(home: Path) -> None:
    """The 2026-08-25 page's `file` field was the fragment

        ===== /home/.../Tdarr_Server_Log.txt (ERROR

    a header with zero lines under it, produced by the byte cap landing inside
    a header. A section that always ends on a line boundary cannot hand a model
    a headless path token.
    """
    # Drive the section past SECTION_CAP through the DATED leg, which applies no
    # per-line cut: 15 lines survive `head -n 15` per file and each is ~260
    # bytes, so the section overflows and collect_cap's cap is the thing that
    # bites. (Driving it through tailnew instead would not reach collect_cap at
    # all - tailnew's own 900-byte window would cap first.)
    logs = home / ".apps" / "tdarr" / "logs"
    today = time.strftime("%Y-%m-%d", time.localtime())
    long_lines = "".join(
        f"[{today}T01:02:03.000] [ERROR] fault{i:03d} " + "x" * 200 + " ENDMARK\n"
        for i in range(20)
    )
    write(logs / "Tdarr_Server_Log.txt", long_lines)
    write(logs / "Tdarr_Node_Log.txt", long_lines)

    tdarr = run_collector(home)["tdarr"]
    assert len(tdarr.encode("utf-8")) <= SECTION_CAP
    assert len(tdarr.encode("utf-8")) > SECTION_CAP - 400, "cap did not actually bite"
    assert tdarr.endswith("\n"), "section must end on a line boundary"
    tail = tdarr.rstrip("\n").splitlines()[-1]
    assert tail.endswith("ENDMARK"), f"last line was truncated mid-line: {tail!r}"


def test_a_short_section_is_not_trimmed(home: Path) -> None:
    """Line-safety must cost nothing when nothing was cut. Truncation is
    detected by reading cap+1 bytes, so an uncut section keeps its last line.
    """
    write(node_err(home), "seed\n")
    seed_watermark(home, node_err(home))
    with node_err(home).open("a", encoding="utf-8", newline="\n") as fh:
        fh.write("first new line\nsecond new line\n")

    tdarr = run_collector(home)["tdarr"]
    assert "first new line" in tdarr
    assert "second new line" in tdarr, "uncut section lost its last line"


# --------------------------------------------------------------------------- #
# The mtime gate is still the fallback for everything else
# --------------------------------------------------------------------------- #
def test_a_frozen_dated_log_still_contributes_nothing(home: Path) -> None:
    """tailfresh's law, unchanged: a decommissioned app's last error must not
    re-alert for ever. sabnzbd.log now also carries the per-line filter, but the
    file-level gate is what must hold when the whole file is old.
    """
    sab = home / ".apps" / "sabnzbd" / "logs" / "sabnzbd.log"
    write(sab, "2026-01-02 03:04:05,000::ERROR::[misc] ancient sab failure\n",
          age_days=FRESH_DAYS + 30)
    assert "ancient sab failure" not in run_collector(home)["sabnzbd"]


def test_a_fresh_file_full_of_stale_dated_lines_contributes_nothing(home: Path) -> None:
    """The append-only case for a DATED source: fresh mtime, old lines. This is
    what the line filter is for, and sabnzbd/nginx were the last two dated
    sources still running without one.
    """
    sab = home / ".apps" / "sabnzbd" / "logs" / "sabnzbd.log"
    write(sab, "2026-01-02 03:04:05,000::ERROR::[misc] ancient sab failure\n")
    out = run_collector(home)["sabnzbd"]
    assert "ancient sab failure" not in out

    now = time.strftime("%Y-%m-%d", time.localtime())
    with sab.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(f"{now} 03:04:05,000::ERROR::[misc] live sab failure\n")
    assert "live sab failure" in run_collector(home)["sabnzbd"]


# ===========================================================================
# Council refuters, 2026-08-25. Both refuted the first cut with high confidence
# and both reproduced their case live. These are those two cases.
# ===========================================================================

def test_a_stale_watermark_is_treated_as_first_sight(home: Path) -> None:
    """REFUTER 1. A byte delta is NOT a time bound.

    The mark says "these bytes are new since the mark" and says nothing about
    how old the mark is. REA is logon/session triggered with
    StartWhenAvailable, and the audit log shows routine off-gaps of 50/59/75/
    76/91/102 hours against FRESH_DAYS=3. On the boot run after such a gap the
    mark is a gap old, so the delta reaches back across the entire gap -- and
    the bytes are undated, so no line filter can touch them. That boot run is
    exactly when the operator gets paged.

    The mtime floor cannot save it: mtime grades the FILE, so a single write
    yesterday clears the floor while the delta still spans days. Hence the mark
    carries its own age, and a mark older than the window emits nothing."""
    err = node_err(home)
    write(err, STALE_EACCES * 4, age_days=0.5)     # file itself looks fresh
    off = home / ".opt" / "maint" / "rea" / "offsets"
    off.parent.mkdir(parents=True, exist_ok=True)
    stale = int(time.time()) - (FRESH_DAYS + 2) * 86400
    off.write_text(f"{_posix(err)} 10 {stale}\n", encoding="utf-8")

    sections = run_collector(home)

    assert "EACCES" not in sections["tdarr"], \
        "a stale mark must not ship the whole off-gap as if it were new"
    assert "1779348004454" not in sections["tdarr"]
    # And it must RE-ARM, or the source goes dark for ever after one long gap.
    assert read_offsets(home).get(_posix(err)) == err.stat().st_size


def test_a_fresh_watermark_still_ships_new_bytes(home: Path) -> None:
    """NEGATIVE CONTROL for the guard above. Steady-state hourly running must
    keep working -- a fix that silences real errors is worse than the noise."""
    err = node_err(home)
    write(err, "boring startup line\n", age_days=0.0)
    off = home / ".opt" / "maint" / "rea" / "offsets"
    off.parent.mkdir(parents=True, exist_ok=True)
    off.write_text(f"{_posix(err)} {err.stat().st_size} {int(time.time())}\n",
                   encoding="utf-8")
    with err.open("a", encoding="utf-8") as fh:
        fh.write("Error: EACCES: permission denied, mkdir '/tdarr-workDir-node-NEW'\n")

    sections = run_collector(home)

    assert "tdarr-workDir-node-NEW" in sections["tdarr"], \
        "a genuinely new error must still reach the models"


def test_a_mid_line_iso_date_is_still_filtered(home: Path) -> None:
    """REFUTER 2. The dated tdarr filter read the date from a FIXED OFFSET
    (substr($0,2,10)), so it only saw a date when the line STARTED with one.

    log4js interleaves stack frames, so a continuation line carries the next
    record's bracketed date mid-line:

        at process.processTicksAndRejections (node:...[2026-05-21T09:57:14] ...

    Those lines sailed past the cutoff and were found live in the shipped blob.
    The filter now matches a bracketed ISO date ANYWHERE in the line."""
    stale_day = "2026-05-21"
    log = home / ".apps" / "tdarr" / "logs" / "Tdarr_Node_Log.txt"
    fresh = time.strftime("%Y-%m-%d", time.gmtime())
    write(log,
          "    at process.processTicksAndRejections (node:internal)"
          f"[{stale_day}T09:57:14.716] [ERROR] Tdarr_Node - FFmpeg failed\n"
          f"[{fresh}T10:00:00.000] [ERROR] Tdarr_Node - fresh real failure\n",
          age_days=0.0)

    sections = run_collector(home)

    assert stale_day not in sections["tdarr"], \
        "a mid-line stale date must be filtered, not just a leading one"
    assert "fresh real failure" in sections["tdarr"], \
        "the fresh line must survive - do not trade noise for blindness"


def test_an_unwritable_watermark_fails_loud_not_dark(home: Path) -> None:
    """COUNCIL 2. If the offsets file cannot be written, `prev` is unreadable
    every run, every run is "first sight", and the source goes SILENTLY AND
    PERMANENTLY DARK -- unbounded loss wearing the costume of the bounded
    one-window trade the header describes. A reviewer produced this by
    replacing the offsets path with a directory.

    It must emit a visible collector-error instead of an innocent empty
    section."""
    err = node_err(home)
    write(err, STALE_EACCES, age_days=0.0)
    off = home / ".opt" / "maint" / "rea" / "offsets"
    off.parent.mkdir(parents=True, exist_ok=True)
    off.mkdir()                       # a directory cannot be replaced by mv

    sections = run_collector(home)

    assert "collector-error" in sections["tdarr"], \
        "an unwritable watermark must be visible, not silently empty"
    assert "EACCES" not in sections["tdarr"], \
        "and it must still withhold the unprovable content"

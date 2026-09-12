"""Tests for scripts/canaries/plex-decision-stable-file.sh.

This canary is the EARNED half of the REA noise class
`plex-vanished-file-decision-failure`. That class silences every Plex
"Failed to get a decision for: <path>" line on the strength of a one-time
census (79 occurrences: 54 files gone, 25 present with an mtime LATER than the
error, 0 present-and-unchanged). A census is history; this canary re-derives it
every hour so the suppression stays true rather than merely having been true.

Which makes the test that matters obvious: **the third bucket must red.** If
`test_a_stable_present_file_reds` ever stops failing on a broken canary, a
member-visible unplayable file goes unreported by BOTH halves, because REA is
deliberately mute about this line shape.

Six jobs:

  1. THE THREE BUCKETS, each proved independently: gone -> benign, replaced
     (mtime later than the error) -> benign, present-and-unchanged -> FINDING.

  2. THE EARNED-ABSENCE RULE (the 2026-08-23 tdarr-ghost lesson). "The file is
     gone" and "I could not look" are the same `[ -e ]` answer and want
     OPPOSITE verdicts. A path outside the media root, or one whose nearest
     surviving ancestor is outside it, is `unreachable` and never `gone`; enough
     of them is exit 2. Remove that rule and an unmounted media tree reads as a
     library that was simply all deleted -- green, on nothing.

  3. THE SERIES-DELETE CASE, which broke the first draft. The reaper deletes a
     whole show, so the season AND show directories go with the file and the
     immediate parent does not exist either. The first draft asked only about
     the immediate parent and scored all ten live occurrences unadjudicable.
     That is what `test_a_whole_series_delete_is_still_provably_gone` pins.

  4. TIMEZONE. PMS logs in UTC while the box runs CEST. Every stamp is parsed
     with `TZ=UTC date -d`; parsing them as local time makes every age two hours
     wrong, which silently changes what falls inside the window.

  5. RULE 5: exit 0 / 1 / 2 are three distinct states. Missing log, unreadable
     log, stale log, bad config and too-many-unreachable are all exit 2, and
     none of them may print a zero count as health.

  6. EVERY BUCKET IS NAMED ON EVERY EXIT PATH, including the zero case, so a
     regression shows up as a number MOVING between buckets rather than as an
     absence.

Requires bash + GNU coreutils (`date -d`, `stat -c`, `touch -d`). Skips cleanly
if absent.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "canaries" / "plex-decision-stable-file.sh"
SYSTEMD_DIR = REPO_ROOT / "scripts" / "maint" / "systemd"
UNIT_STEM = "manitoba-maint-canary-plex-decision-stable-file"


def _has_gnu_tools() -> bool:
    if shutil.which("bash") is None:
        return False
    try:
        p = subprocess.run(
            ["bash", "-c", "date -u -d '2026-01-01T00:00:00Z' +%s && stat -c %Y ."],
            capture_output=True, text=True, timeout=20)
    except Exception:
        return False
    return p.returncode == 0 and p.stdout.split("\n")[0].strip() == "1767225600"


pytestmark = pytest.mark.skipif(
    not _has_gnu_tools(),
    reason="plex-decision-stable-file.sh needs bash + GNU date/stat on PATH",
)

# PMS stamps are UTC. NOW is an hour after the fixture errors.
NOW = "2026-09-09 06:00:00"
ERR_STAMP = "Sep 09, 2026 05:04:52.049"


def _fail_line(path: str, stamp: str = ERR_STAMP) -> str:
    """The real PMS shape, verbatim from the 2026-09-09 page."""
    return f"{stamp} [139868285422392] ERROR - Failed to get a decision for: {path}"


def _tick(stamp: str = "Sep 09, 2026 05:59:00.000") -> str:
    return f"{stamp} [139868385549112] INFO - Library section 2 will be updated"


class Fixture:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.media = tmp_path / "media"
        self.media.mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "plex.log"
        self.lines: list[str] = [_tick()]

    def episode(self, show: str, *, exists: bool, mtime: str | None = None) -> str:
        """A path under <media>/TV Shows/<show>/Season 1/. `mtime` is a
        `touch -d` string; None means "now", i.e. later than the error."""
        d = self.media / "TV Shows" / show / "Season 1"
        p = d / f"{show} - S01E01 WEBDL-1080p.mkv"
        if exists:
            d.mkdir(parents=True, exist_ok=True)
            p.touch()
            if mtime:
                subprocess.run(["touch", "-d", mtime, str(p)], check=True, timeout=20)
        return str(p).replace("\\", "/")

    def add(self, path: str, stamp: str = ERR_STAMP) -> None:
        self.lines.append(_fail_line(path, stamp))

    def write(self) -> None:
        # A real PMS log is append-only and therefore chronological, which is
        # why the canary may take the LAST timestamped line as the newest. The
        # fixture must honour that invariant or it tests a state Plex cannot
        # produce: an out-of-order tail made the freshness probe read a
        # four-day-old error line as "newest" and report a stale log.
        import datetime as _dt

        def _key(ln: str):
            try:
                return _dt.datetime.strptime(ln[:21], "%b %d, %Y %H:%M:%S")
            except ValueError:
                return _dt.datetime.min

        body = "\n".join(sorted(self.lines, key=_key))
        self.log.write_text(body + "\n", encoding="utf-8", newline="\n")

    def run(self, **env_overrides):
        self.write()
        env = dict(os.environ)
        env.update({
            "PLEX_DECISION_LOG": str(self.log),
            "PLEX_DECISION_MEDIA_ROOT": str(self.media).replace("\\", "/"),
            "PLEX_DECISION_NOW": NOW,
            "PLEX_DECISION_TRAIL": str(self.root / "trail.log"),
        })
        env.update(env_overrides)
        return subprocess.run(["bash", str(SCRIPT)], env=env,
                              capture_output=True, text=True, timeout=60)


def _stage(res) -> str | None:
    m = re.search(r"STAGE=([a-z0-9-]+)", res.stderr or "")
    return m.group(1) if m else None


def _bucket(res, name: str) -> int:
    m = re.search(name + r"=(\d+)", (res.stdout or "") + (res.stderr or ""))
    assert m, f"{name} not reported on this exit path: {res.stdout!r} {res.stderr!r}"
    return int(m.group(1))


# ---------------------------------------------------------------------------
# 1. The three buckets
# ---------------------------------------------------------------------------
def test_a_deleted_file_is_benign(tmp_path):
    f = Fixture(tmp_path)
    # The show directory still exists (another episode survives), the file does not.
    (f.media / "TV Shows" / "Shrinking" / "Season 1").mkdir(parents=True)
    f.add(str(f.media / "TV Shows" / "Shrinking" / "Season 1" / "gone.mkv").replace("\\", "/"))
    res = f.run()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _bucket(res, "gone") == 1
    assert _bucket(res, "unreachable") == 0


def test_a_file_rewritten_after_the_error_is_benign(tmp_path):
    """25 of the 79 censused occurrences are this: the path was absent or
    mid-write when MDE read it and was rewritten afterwards. Law & Order S01
    errored 2026-08-08 18:19 and the files landed 2026-08-09 03:46."""
    f = Fixture(tmp_path)
    f.add(f.episode("Law & Order", exists=True))  # mtime = now, after the error
    res = f.run()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _bucket(res, "replaced") == 1


def test_a_stable_present_file_reds(tmp_path):
    """THE TEST THAT MATTERS. The census found this bucket empty; REA is
    deliberately mute about the line shape, so if this canary stops reding here
    a member-visible unplayable file is reported by nothing at all."""
    f = Fixture(tmp_path)
    f.add(f.episode("Frieren", exists=True, mtime="2026-09-08 00:00:00"))
    res = f.run()
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert _stage(res) == "plex-decision-stable-file"
    assert "Frieren" in res.stderr, res.stderr
    assert _bucket(res, "seen") == 1


def test_the_three_buckets_are_counted_separately(tmp_path):
    f = Fixture(tmp_path)
    (f.media / "TV Shows" / "Gone" / "Season 1").mkdir(parents=True)
    f.add(str(f.media / "TV Shows" / "Gone" / "Season 1" / "x.mkv").replace("\\", "/"))
    f.add(f.episode("Replaced", exists=True))
    f.add(f.episode("Stable", exists=True, mtime="2026-09-08 00:00:00"))
    res = f.run()
    assert res.returncode == 1
    assert (_bucket(res, "seen"), _bucket(res, "gone"), _bucket(res, "replaced")) == (3, 1, 1)


# ---------------------------------------------------------------------------
# 2 + 3. Earned absence
# ---------------------------------------------------------------------------
def test_a_whole_series_delete_is_still_provably_gone(tmp_path):
    """The case that broke the first draft: the reaper deletes the SERIES, so
    the season and show directories go with the file and the immediate parent
    does not exist either. Asking only about the parent scored all ten live
    occurrences `unreachable` -- a canary that cannot assert is not a canary.
    The nearest surviving ancestor here is <media>/TV Shows."""
    f = Fixture(tmp_path)
    (f.media / "TV Shows").mkdir(parents=True)
    f.add(str(f.media / "TV Shows" / "Shrinking" / "Season 1" / "s.mkv").replace("\\", "/"))
    res = f.run()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _bucket(res, "gone") == 1
    assert _bucket(res, "unreachable") == 0


def test_an_unmounted_media_tree_is_cannot_assert_not_clean(tmp_path):
    """If the media root itself is gone, every path is trivially 'absent'. An
    absence-only rule would report a library that had vanished as a library
    that had simply been emptied -- green, on nothing. That is strictly worse
    than the false red it replaces."""
    f = Fixture(tmp_path)
    for show in ("A", "B", "C"):
        f.add(str(f.media / "TV Shows" / show / "Season 1" / "x.mkv").replace("\\", "/"))
    f.write()
    shutil.rmtree(f.media)  # the mount point is gone
    res = f.run()
    assert res.returncode == 2, (res.stdout, res.stderr)
    assert _stage(res) == "plex-decision-unreachable"
    assert "PASS" not in res.stdout
    assert _bucket(res, "gone") == 0


def test_a_path_outside_the_media_root_is_never_gone(tmp_path):
    """Refuse to guess about a tree we do not own."""
    f = Fixture(tmp_path)
    for n in range(3):
        f.add(f"/somewhere/else/entirely/{n}.mkv")
    res = f.run()
    assert res.returncode == 2
    assert _stage(res) == "plex-decision-unreachable"
    assert _bucket(res, "unreachable") == 3


def test_unreachable_under_the_tolerance_does_not_red(tmp_path):
    """One odd path is not an outage; the default tolerance is 2. It must still
    be COUNTED AND NAMED rather than quietly folded into `gone`."""
    f = Fixture(tmp_path)
    f.add("/somewhere/else/entirely/one.mkv")
    res = f.run()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _bucket(res, "unreachable") == 1
    assert _bucket(res, "gone") == 0


# ---------------------------------------------------------------------------
# 4. Timezone
# ---------------------------------------------------------------------------
def test_pms_stamps_are_parsed_as_utc_not_box_local(tmp_path):
    """PMS logs UTC, the box runs CEST (+2). Parsed as local, an error two
    hours before the window edge lands on the wrong side of it. This fixture
    sits 25h before NOW with a 26h window: correct under UTC, excluded if the
    stamp were read as CEST and the clock as UTC (or vice versa)."""
    f = Fixture(tmp_path)
    f.lines = [_tick("Sep 09, 2026 05:59:00.000")]
    f.add(f.episode("Edge", exists=True, mtime="2026-09-07 00:00:00"),
          stamp="Sep 08, 2026 05:00:00.000")   # 25h before NOW
    res = f.run(TZ="Europe/Amsterdam")
    assert res.returncode == 1, (res.stdout, res.stderr)
    assert _bucket(res, "seen") == 1
    body = SCRIPT.read_text(encoding="utf-8")
    assert "TZ=UTC date -d" in body, "the UTC parse is the mechanism; do not drop it"


def test_an_error_older_than_the_window_is_not_considered(tmp_path):
    f = Fixture(tmp_path)
    f.add(f.episode("Ancient", exists=True, mtime="2026-09-01 00:00:00"),
          stamp="Sep 05, 2026 05:00:00.000")
    res = f.run()
    assert res.returncode == 0, (res.stdout, res.stderr)
    assert _bucket(res, "seen") == 0
    # Guard the guard: widen the window and the same line is a finding.
    assert f.run(PLEX_DECISION_WINDOW_H="200").returncode == 1


# ---------------------------------------------------------------------------
# 5. Three states, never two
# ---------------------------------------------------------------------------
def test_missing_log_is_cannot_assert(tmp_path):
    f = Fixture(tmp_path)
    res = f.run(PLEX_DECISION_LOG=str(tmp_path / "nope.log"))
    assert res.returncode == 2
    assert _stage(res) == "plex-log-missing"
    assert "PASS" not in res.stdout


def test_stale_log_is_cannot_assert(tmp_path):
    """Plex writes constantly. A log that has gone quiet means a zero count
    carries no information about whether decisions are failing."""
    f = Fixture(tmp_path)
    f.lines = [_tick("Sep 09, 2026 01:00:00.000")]  # 5h before NOW, budget 120m
    res = f.run()
    assert res.returncode == 2
    assert _stage(res) == "plex-log-stale"
    assert "PASS" not in res.stdout


def test_bad_numeric_config_is_cannot_assert(tmp_path):
    for knob in ("PLEX_DECISION_WINDOW_H", "PLEX_DECISION_STALE_BUDGET_MIN"):
        for bad in ("abc", "0", "-1", "2.5"):
            f = Fixture(tmp_path / f"{knob}{bad}".replace(".", "_"))
            f.add(f.episode("X", exists=True, mtime="2026-09-08 00:00:00"))
            res = f.run(**{knob: bad})
            assert res.returncode == 2, (knob, bad, res.stdout, res.stderr)
            assert _stage(res) == "plex-canary-bad-config", (knob, bad)
            assert "PASS" not in res.stdout, (knob, bad)


def test_unparseable_now_override_is_cannot_assert(tmp_path):
    f = Fixture(tmp_path)
    res = f.run(PLEX_DECISION_NOW="not-a-date")
    assert res.returncode == 2
    assert _stage(res) == "plex-canary-bad-config"


# ---------------------------------------------------------------------------
# 6. Every bucket, every exit path
# ---------------------------------------------------------------------------
def test_the_clean_case_still_names_every_bucket(tmp_path):
    """A vacuous pass and a real pass must be distinguishable at a glance."""
    f = Fixture(tmp_path)
    res = f.run()
    assert res.returncode == 0
    for bucket in ("seen", "gone", "replaced", "unreachable"):
        assert _bucket(res, bucket) == 0
    assert "window=26h" in res.stdout


def test_every_non_pass_writes_the_durable_trail(tmp_path):
    f = Fixture(tmp_path)
    f.add(f.episode("Trail", exists=True, mtime="2026-09-08 00:00:00"))
    assert f.run().returncode == 1
    assert f.run(PLEX_DECISION_LOG=str(tmp_path / "nope.log")).returncode == 2
    body = (tmp_path / "trail.log").read_text(encoding="utf-8")
    assert "plex-decision-stable-file" in body
    assert "plex-log-missing" in body


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------
def test_systemd_units_exist_and_are_consistent():
    svc = (SYSTEMD_DIR / (UNIT_STEM + ".service")).read_text(encoding="utf-8")
    tmr = (SYSTEMD_DIR / (UNIT_STEM + ".timer")).read_text(encoding="utf-8")
    assert "canary push plex-decision-stable-file" in svc
    assert "OnCalendar=hourly" in tmr
    assert "Persistent=true" in tmr


def test_header_documents_the_defaults_the_code_uses():
    body = SCRIPT.read_text(encoding="utf-8")
    assert "PLEX_DECISION_WINDOW_H:-26}" in body
    assert "PLEX_DECISION_STALE_BUDGET_MIN:-120}" in body
    assert "PLEX_DECISION_UNREACHABLE_MAX:-2}" in body
    assert "WINDOW is 26h" in body
    assert "default 120" in body
    assert "default 2" in body


def test_it_names_the_noise_class_it_is_the_backstop_for():
    """The whole point. If the class is ever renamed or removed, this is the
    string that says the two halves have come apart."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "plex-vanished-file-decision-failure" in body
    ledger = (REPO_ROOT / "manifest" / "rea-noise-classes.yaml").read_text(encoding="utf-8")
    assert "plex-vanished-file-decision-failure" in ledger
    assert "plex-decision-stable-file" in ledger, (
        "the noise class must name the canary that keeps it honest, or the "
        "suppression is defended by a census again")

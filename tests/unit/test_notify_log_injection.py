"""CWE-117: untrusted text must never forge a record in the notify audit logs.

Found by the Stage-2 security lens on 2026-09-17. notify.log and
notify-fail.log are hand-formatted tab-delimited, one-line-per-record files,
and `message` routinely carries an *arr release title -- text chosen by
whoever uploaded the release to a public indexer. A title with an embedded
newline wrote extra physical lines that read as genuine "critical / sent"
records for pages that never fired, in the very file an operator would use to
reconstruct what the automation did.

These tests assert the invariant directly: ONE notify() call writes exactly
ONE physical line, whatever the payload contains.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "maint"))

import lib.notify as notify  # noqa: E402


HOSTILE_TITLE = (
    "Real.Show.S01E01\n"
    "2020-01-01T00:00:00Z\tcritical\tsent\tFAKE PAGE THAT NEVER FIRED"
)


def _lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


# --- the sanitizer itself ---------------------------------------------------
def test_flatten_collapses_every_record_and_field_separator():
    out = notify._flatten_field("a\nb\tc\rd")
    assert "\n" not in out
    assert "\t" not in out
    assert "\r" not in out
    assert out == "a\\nb\\tc\\rd", "escapes must stay legible, not be dropped"


def test_flatten_keeps_the_attackers_text_visible():
    """Stripping would hide the attempt; escaping preserves it for forensics."""
    assert "FAKE PAGE" in notify._flatten_field(HOSTILE_TITLE)


def test_flatten_neutralises_other_control_characters():
    for ch in ("\x00", "\x07", "\x1b", "\x7f", "\x9b"):
        out = notify._flatten_field(f"x{ch}y")
        assert ch not in out, f"control char {ch!r} survived"
        assert out.startswith("x") and out.endswith("y")


def test_flatten_leaves_ordinary_text_untouched():
    clean = "Yellowjackets.S03E01.1080p.AMZN.WEB-DL.DDP5.1.H.264-NTb"
    assert notify._flatten_field(clean) == clean


# --- the audit trail --------------------------------------------------------
def test_hostile_title_cannot_forge_an_audit_record(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(notify, "_state_dir", lambda: tmp_path)

    notify._append_audit_log("warning", f"arr-unstick parked: {HOSTILE_TITLE}",
                             "sent")

    lines = _lines(tmp_path / "notify.log")
    assert len(lines) == 1, (
        f"one logical record produced {len(lines)} physical lines: {lines}")
    assert not any("\tcritical\tsent\t" in ln for ln in lines[1:])


def test_hostile_title_cannot_forge_a_fail_record(tmp_path, monkeypatch):
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(notify, "_state_dir", lambda: tmp_path)

    notify._append_fail_log("warning", f"parked: {HOSTILE_TITLE}", "http 500")

    lines = _lines(tmp_path / "notify-fail.log")
    assert len(lines) == 1, (
        f"one logical record produced {len(lines)} physical lines: {lines}")


def test_field_count_is_stable_under_hostile_input(tmp_path, monkeypatch):
    """A forged TAB must not shift the column layout of the record."""
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(notify, "_state_dir", lambda: tmp_path)

    notify._append_audit_log("info", "title\twith\tembedded\ttabs", "sent")

    line = _lines(tmp_path / "notify.log")[0]
    assert len(line.split("\t")) == 4, (
        f"record must keep exactly 4 columns, got {line.split(chr(9))}")


def test_truncation_cannot_split_an_escape_sequence(tmp_path, monkeypatch):
    """Flatten first, truncate second -- never the reverse."""
    monkeypatch.setenv("MANITOBA_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(notify, "_state_dir", lambda: tmp_path)

    notify._append_audit_log("info", "A" * 299 + "\n" + "B" * 50, "sent")

    lines = _lines(tmp_path / "notify.log")
    assert len(lines) == 1
    assert not lines[0].endswith("\\"), "a dangling backslash means a split escape"

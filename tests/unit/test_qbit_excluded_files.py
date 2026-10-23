"""QFLX-71: qBittorrent must never download disc images or executables.

2026-10-22 three fake "episodes" were one identical Windows PE executable named
.iso; unpackerr panicked extracting it and crash-looped for 5 hours. The script
(59c-qbit-excluded-files.py) arms qBit's excluded_file_names. These pin the
merge: it only ever ADDS, is case-insensitive, and is idempotent.

Offline. Loaded by path because the filename starts with a digit.
"""
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "configure" / "59c-qbit-excluded-files.py"


def _load():
    spec = importlib.util.spec_from_file_location("qbit_excluded_files", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


def test_the_2026_10_22_vector_is_covered():
    assert "*.iso" in m.REQUIRED_PATTERNS
    assert "*.exe" in m.REQUIRED_PATTERNS


def test_disabled_client_gets_armed():
    p = m.plan({"excluded_file_names_enabled": False, "excluded_file_names": ""})
    assert p["excluded_file_names_enabled"] is True
    assert m.split_patterns(p["excluded_file_names"]) == list(m.REQUIRED_PATTERNS)


def test_operator_patterns_are_kept_and_first():
    p = m.plan({"excluded_file_names_enabled": True,
                "excluded_file_names": "*sample*\r\n*.txt\n"})
    got = m.split_patterns(p["excluded_file_names"])
    assert got[:2] == ["*sample*", "*.txt"]
    assert set(m.REQUIRED_PATTERNS) <= set(got)


def test_case_insensitive_no_duplicates():
    merged, added = m.merge_patterns("*.ISO\n*.Exe")
    assert "*.iso" not in added and "*.exe" not in added
    assert len([x for x in merged if x.lower() == "*.iso"]) == 1


def test_armed_client_is_unchanged():
    armed = {"excluded_file_names_enabled": True,
             "excluded_file_names": "\n".join(m.REQUIRED_PATTERNS)}
    assert m.plan(armed) is None
    assert m.missing(armed) == []


def test_enabled_flag_alone_is_not_armed():
    prefs = {"excluded_file_names_enabled": True, "excluded_file_names": "*.iso"}
    left = m.missing(prefs)
    assert "*.exe" in left and "*.iso" not in left
    assert m.missing({"excluded_file_names": "\n".join(m.REQUIRED_PATTERNS)}) == \
        ["excluded_file_names_enabled=false"]

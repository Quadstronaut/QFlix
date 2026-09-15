"""OPERATOR RULING 2026-09-15: watchlist auto-request bits are never written."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "maint"))
from lib import seerrusers as SU  # noqa: E402


def test_member_baseline_carries_no_auto_request_bit():
    assert SU.MEMBER_PERMISSIONS & SU.PERMISSIONS_NEVER == 0
    assert SU.MEMBER_PERMISSIONS == 1149247648
    # everything else the old baseline granted is untouched
    assert SU.MEMBER_PERMISSIONS | SU.PERMISSIONS_NEVER == 1155539104 | 1048576


def test_never_bits_are_exactly_the_three_watchlist_permissions():
    assert SU.PERMISSIONS_NEVER == 1048576 | 2097152 | 4194304

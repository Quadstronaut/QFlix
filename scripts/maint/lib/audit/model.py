"""lib/audit/model.py — the value types, the canonical body, and the digest.

Everything a detector emits flows through here, which is why the digest-hygiene
rules are enforced HERE as a production invariant rather than only in a test:
if a detector ever leaks an absolute path, a hostname, a timestamp or a PID
into a finding, the audit exits 2 (REGIME INTEGRITY) instead of quietly
producing a digest that changes on every run for no defensible reason.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

# Verdict statuses. A detector emits one verdict per instance in its boundary —
# including the boring ones. `ok` instances are counted, never listed, which is
# how "enumerates 100% of the boundary" stays affordable.
OK = "ok"
FINDING = "finding"
WAIVED = "waived"

# Severity, assigned by the engine from the class's status + enforced_kinds.
ENFORCED = "enforced"
ADVISORY = "advisory"


class RegimeError(Exception):
    """The AUDITOR is broken (bad ledger, broken bijection, dirty digest).

    Deliberately distinct from "the auditor found something". A broken auditor
    that looks like a clean audit is the single worst failure this system can
    have, so it gets its own exception, its own exit code (2) and its own Kuma
    semantics.
    """


@dataclass(frozen=True, order=True)
class Verdict:
    """One instance inside a detector's enumeration boundary.

    `instance_id` must be stable across runs and across machines: it is the
    join key for waivers and it is digested. Convention:
    "<repo-relative-path>:<lineno>:<kind>" or "<logical-key>:<kind>" where no
    file applies. Never an absolute path, never a timestamp.
    """
    instance_id: str
    kind: str
    status: str = OK
    path: str = ""
    lineno: int = 0
    detail: str = ""


@dataclass
class DetectorResult:
    """What one detector returns: every instance it looked at, plus counters."""
    boundary_name: str
    boundary_size: int
    verdicts: List[Verdict] = field(default_factory=list)
    metrics: Dict[str, int] = field(default_factory=dict)

    def sorted_verdicts(self) -> List[Verdict]:
        return sorted(self.verdicts, key=lambda v: (v.path, v.lineno, v.instance_id, v.kind))


# ---------------------------------------------------------------------------
# Digest hygiene
# ---------------------------------------------------------------------------
# Each rule is (name, compiled regex). A match anywhere in the canonical body
# is a RegimeError. These are intentionally strict: it is far better to reject
# a legitimate-but-unusual detail string (and force the detector author to make
# it terse) than to ship a digest that churns.
_DIRTY: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # C:\... or C:/... — a Windows absolute path.
    ("windows-absolute-path", re.compile(r"[A-Za-z]:[\\/]")),
    # POSIX absolute paths into the usual roots. Anchored on a delimiter so
    # "and/or" style prose cannot trip it.
    ("posix-absolute-path", re.compile(r'(?:^|["\s(\[])/(?:home|root|usr|var|etc|tmp|opt|mnt|srv)/')),
    # A home-relative box path is just as non-portable as an absolute one.
    ("home-relative-path", re.compile(r"~/")),
    # ISO date-TIME. A bare date (2026-07-29) is deliberately allowed: waiver
    # ids and class ids may carry one and they are stable inputs, not clocks.
    ("iso-timestamp", re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")),
    ("clock-time", re.compile(r"\b\d{2}:\d{2}:\d{2}\b")),
    ("pid", re.compile(r"(?i)\bpid[=: ]\s*\d+")),
    # FQDN-shaped tokens. The TLD set deliberately excludes every file
    # extension in this repo, so "qflix-faq.html" and "movie.sh" are safe while
    # "seedbox.example.com" is not.
    ("hostname", re.compile(r"(?i)\b[a-z0-9][a-z0-9-]*\.(?:me|com|net|org|app|io|dev|local|lan|internal)\b")),
    ("ip-literal", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
)


def assert_digest_hygiene(canonical: str) -> None:
    """Raise RegimeError naming the first rule the canonical body violates."""
    for name, rx in _DIRTY:
        m = rx.search(canonical)
        if m:
            start = max(0, m.start() - 60)
            raise RegimeError(
                "digest hygiene violated (" + name + "): canonical body contains "
                + repr(m.group(0)) + " near ..." + canonical[start:m.end() + 40] + "..."
            )


def canonical_json(body: Any) -> str:
    """The exact bytes the digest is taken over.

    sort_keys makes dict-iteration order irrelevant; the tight separators make
    whitespace irrelevant; ensure_ascii makes the platform's default encoding
    irrelevant. Callers must have already sorted every LIST — key order is not
    the same thing as list order and only the caller knows the right sort.
    """
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(body: Any) -> str:
    canonical = canonical_json(body)
    assert_digest_hygiene(canonical)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()

"""M-2 from the money-path council merge (wf_d43e030e-347, 2026-08-08).

D-7 found ONE unguarded PII field; the arbiter's D-8 found the defect was
THREE, because the security artifact probed a single line. The lesson is
structural: a per-field assertion is exactly the kind of test that misses the
next field somebody adds. So this is a REFLECTIVE SWEEP -- it walks every
dataclass in lib/entitlement.py and lib/payer_oracle.py and refuses any field
that carries raw member addresses (or raw response bodies that embed them)
unless that field is repr=False.

Why repr matters at all: a traceback, a pytest failure message, an f-string
debug line -- anything that repr()s one of these objects -- becomes a durable,
forwarded string (journald, Discord, Kuma msg=). SPEC section 4 says raw
addresses never leave the process unmasked; repr=False is the structural half
of that promise.
"""
from __future__ import annotations

import dataclasses
import os
import sys

LIB = os.path.abspath(os.path.join(os.path.dirname(__file__),
                                   "..", "..", "scripts", "maint", "lib"))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import entitlement  # noqa: E402
import payer_oracle  # noqa: E402

# Field names that hold raw member addresses or raw bodies embedding them.
# NAME-based on purpose: the three fields D-7/D-8 caught had three different
# types (List[str], Sequence[str], str), so a type test cannot draw the line.
PII_FIELD_NAMES = frozenset({
    "holder",      # a billing.holder address
    "entitled",    # lists of addresses the service reports
    "raw",         # raw response body; may embed the queried address
    "email",
    "accounts",
    "addresses",
})

MODULES = (entitlement, payer_oracle)

# The three fields the council actually caught, as (module, class, field).
# The sweep must at minimum rediscover these: if a refactor renames or moves
# them, this test must fail loudly rather than pass vacuously.
KNOWN_PII_FIELDS = {
    ("entitlement", "BulkAnswer", "entitled"),
    ("payer_oracle", "DeclaredPayer", "holder"),
    ("payer_oracle", "BulkFacts", "entitled"),
}


def _all_dataclasses(module):
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and dataclasses.is_dataclass(obj) \
                and obj.__module__ == module.__name__:
            yield obj


def _pii_fields():
    for module in MODULES:
        for cls in _all_dataclasses(module):
            for f in dataclasses.fields(cls):
                if f.name in PII_FIELD_NAMES:
                    yield module.__name__, cls.__name__, f


def test_every_pii_field_in_both_modules_is_repr_false():
    leaks = ["%s.%s.%s" % (m, c, f.name)
             for m, c, f in _pii_fields() if f.repr]
    assert not leaks, (
        "PII fields reachable through repr() -- a traceback or debug line "
        "would forward raw member addresses: %s" % ", ".join(leaks))


def test_the_sweep_still_finds_the_fields_the_council_caught():
    found = {(m, c, f.name) for m, c, f in _pii_fields()}
    missing = KNOWN_PII_FIELDS - found
    assert not missing, (
        "reflective sweep no longer sees %s -- if these fields were renamed "
        "or moved, update KNOWN_PII_FIELDS so the sweep cannot go vacuous"
        % sorted(missing))


def test_answer_raw_stays_guarded():
    """The pre-existing convention the council transferred FROM: Answer.raw
    was the one field already done right. Keep it that way explicitly."""
    f = {x.name: x for x in dataclasses.fields(entitlement.Answer)}["raw"]
    assert f.repr is False

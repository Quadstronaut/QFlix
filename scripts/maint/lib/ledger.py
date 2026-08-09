"""lib/ledger.py -- the money-in ledger the access gate reads.

WHY A LEDGER AND NOT AN API CALL
The status field an access gate wants -- "is this person paid up right now" --
only exists on rails that require a merchant contract. Every rail that does NOT
require one (person-to-person transfer, a tip platform, a bank credit) can tell
you that money ARRIVED and nothing else. There is no subscription object to
read and no cancellation event to wait for.

So "paid up" here is not a field. It is an INFERENCE: a credit of at least the
expected amount, arriving within a rolling window. That inversion is the whole
design, and it has one large advantage -- the ingestion source becomes
swappable. Venmo email today, PayPal tomorrow, a Ko-fi webhook, or the operator
typing it in by hand. The gate never learns which.

APPEND-ONLY
Events are never edited or deleted, only appended. A ledger you can rewrite is
one where "why did this person get cut off in March" has no answer. Corrections
are new events, same as double-entry bookkeeping.

IDEMPOTENCY
Every event carries (source, external_id). Re-reading the same inbox, replaying
a webhook, or re-running after a crash must not extend anyone's access twice.
The pair is the primary key and duplicates are dropped on read, not on write --
writing is allowed to be sloppy so that ingestion can be crash-simple.

LAPSE IS INFERRED FROM SILENCE
No rail here will tell you someone cancelled. Absence of a renewal by the
expected date IS the signal. That means a broken ingester and a genuinely
lapsed household look identical from in here -- which is why this module reports
`stale_ingest` and the gate must refuse to revoke on it. See gate_inputs().
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

# A month is not a fixed number of days and nobody's bank cares. 31 days gives
# a household whose payment lands on the 31st a full cycle before it is judged
# late, and costs at most a few days of grace on a short month. Erring toward
# "still paid" is the correct direction when the alternative is dark TV.
CYCLE_DAYS = 31


class LedgerError(Exception):
    """Raised when the ledger file itself is unreadable or malformed."""


@dataclass(frozen=True)
class Credit:
    """One arrival of money. Immutable by construction."""
    household: str
    source: str            # "venmo" | "paypal" | "kofi" | "manual" | ...
    external_id: str       # the rail's own id for this payment. Dedup key.
    amount_usd: float
    at: str                # ISO-8601 date, UTC
    note: str = ""

    @property
    def day(self) -> date:
        return _as_date(self.at)


def _as_date(v) -> date:
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    s = str(v).strip()
    # Accept a bare date or a full timestamp; rails are inconsistent and this is
    # not the place to be strict about a trailing Z.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            raise LedgerError("unparseable ledger date: %r" % v)


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def append(path: Path, credit: Credit) -> None:
    """Append one credit. Crash-safe enough: one line, one flush, one fsync.

    Deliberately does NOT check for duplicates. Ingestion runs unattended and
    should be as dumb as possible; dedup happens on read where it can be tested
    in isolation. A double-write costs a wasted line, never a wrong answer.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(asdict(credit), sort_keys=True) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read(path: Path) -> List[Credit]:
    """Load every credit, newest last, duplicates removed.

    A malformed line is FATAL, not skipped. A ledger that quietly drops the
    lines it cannot parse will under-report payments, and under-reporting
    payments is how a paying member loses access -- the precise failure this
    whole subsystem exists to prevent. Better to fail the run loudly.
    """
    if not path.exists():
        return []
    out: List[Credit] = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
                c = Credit(
                    household=row["household"],
                    source=row["source"],
                    external_id=row["external_id"],
                    amount_usd=float(row["amount_usd"]),
                    at=row["at"],
                    note=row.get("note", ""),
                )
                c.day  # force a date parse now, not at compare time
            except (ValueError, KeyError, TypeError, LedgerError) as e:
                raise LedgerError("%s line %d is malformed (%s). Refusing to "
                                  "run on a partially-read ledger -- a dropped "
                                  "line under-reports payments and cuts off "
                                  "someone who paid." % (path, n, e))
            key = (c.source, c.external_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    out.sort(key=lambda c: c.day)
    return out


# ---------------------------------------------------------------------------
# The inference
# ---------------------------------------------------------------------------

@dataclass
class Standing:
    household: str
    last_credit: Optional[date]
    last_amount: Optional[float]
    paid_through: Optional[date]
    grace_until: Optional[date]
    state: str            # "paid" | "grace" | "lapsed" | "never_paid"
    shortfall: bool       # last payment was under the expected amount


def standing(credits: Iterable[Credit], household: str, expected_usd: Optional[float],
             grace_days: int, on: Optional[date] = None) -> Standing:
    """Where does one household stand, as of `on`.

    `expected_usd` of None means the amount is UNSET (not free) -- the roster
    validator already refuses to arm in that case, so here it only affects the
    shortfall flag, never the state.

    A short payment does NOT by itself mean lapsed. Somebody sending $45 of a
    $50 agreement is a conversation, not a revocation; the gate surfaces
    `shortfall` and leaves the judgement to a human.
    """
    on = on or today_utc()
    mine = [c for c in credits if c.household == household]
    if not mine:
        return Standing(household, None, None, None, None, "never_paid", False)

    last = max(mine, key=lambda c: c.day)
    paid_through = last.day + timedelta(days=CYCLE_DAYS)
    grace_until = paid_through + timedelta(days=grace_days)

    if on <= paid_through:
        state = "paid"
    elif on <= grace_until:
        state = "grace"
    else:
        state = "lapsed"

    short = expected_usd is not None and last.amount_usd + 1e-9 < expected_usd
    return Standing(household, last.day, last.amount_usd, paid_through,
                    grace_until, state, short)


def last_ingest(credits: Iterable[Credit]) -> Optional[date]:
    """Most recent credit from ANY household -- the ingester's heartbeat."""
    days = [c.day for c in credits]
    return max(days) if days else None


def gate_inputs(credits: List[Credit], stale_after_days: int,
                on: Optional[date] = None) -> tuple:
    """(safe_to_revoke, reason). The interlock that prevents a mass cut-off.

    THE FAILURE THIS EXISTS FOR
    A broken ingester and "nobody paid this month" are indistinguishable from
    inside this module -- both look like an empty recent window. An IMAP
    password change, a Gmail filter edit, or a rail quietly altering its receipt
    format would each read as thirteen simultaneous lapses and revoke the entire
    membership in one run.

    So silence is an ALERT condition, never an ACT condition. If nothing has
    arrived in `stale_after_days`, the gate must report loudly and change
    nothing. Real lapses will still be there tomorrow; a mass revocation will
    not be undone by an apology.
    """
    on = on or today_utc()
    seen = last_ingest(credits)
    if seen is None:
        return False, ("ledger is empty -- cannot distinguish 'nobody has paid' "
                       "from 'ingestion never worked'")
    age = (on - seen).days
    if age > stale_after_days:
        return False, ("no credit from ANY household in %d days (last %s). "
                       "That looks like a broken ingester, not %d simultaneous "
                       "lapses -- refusing to revoke."
                       % (age, seen.isoformat(), 0))
    return True, "ingest fresh (last credit %s, %d day(s) ago)" % (seen.isoformat(), age)


def summarise(credits: List[Credit]) -> Dict[str, int]:
    by_source: Dict[str, int] = {}
    for c in credits:
        by_source[c.source] = by_source.get(c.source, 0) + 1
    return by_source

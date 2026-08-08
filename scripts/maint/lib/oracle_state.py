"""lib/oracle_state.py -- durable memory for the L1 declared-payer clock.

Pure stdlib. Remembers ONE thing: the instant a household FIRST became a
declared payer (SPEC section 3, L1 -- "the oldest declaration is older than
the settle window"). Nothing else in this repo tracks that instant:
members.yaml is a snapshot of CURRENT roster state with no history, so "how
long has this household been declared" only exists if something writes it
down the first time it is observed. This is the same design law
lib/access_state.py documents for the lapse and arrival clocks, applied to a
different clock about a different thing.

ITS OWN FILE, ITS OWN MODULE
-----------------------------
Deliberately not a new field bolted onto lib/access_state.py. That module's
clocks are about one Plex ACCOUNT's acceptance and entitlement history; this
one is about one HOUSEHOLD's billing declaration, a different key space with
a different lifecycle (a household can exist with no accepted share yet, or
with billing changed hands). The qflix-compartmentalize-for-migration
operator law is that unrelated maintenance concerns get their own swappable
unit even when the cadence overlaps -- conflating them here would make this
concern harder to detach from access_state.py during the eventual server
migration, for a cadence overlap that is coincidental, not structural.

LOSS IS SAFE, AND SAFE IN ONLY ONE DIRECTION
----------------------------------------------
A missing or corrupt file re-anchors every currently-declared household's
clock to `now` on the next observe(). That can only ever make the SETTLING
window (payer_oracle.judge() row 6) LONGER -- a household declared for months
briefly reads as "just declared" and gets a couple of green, quiet days it
did not strictly need. It can never make UNPROVEN_BLIND or UNPROVEN_EMPTY
fire EARLY, and it can never manufacture a false PROVEN. That asymmetry is
deliberate: the failure mode of a lost oracle clock must be "wait a little
longer before alerting", never "alert on data that no longer means what it
did", nor "go quiet about a real problem".
"""
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt
import json
import os
from pathlib import Path
from typing import Dict, Iterable, Optional

SCHEMA = 1


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_iso(t: Optional[dt.datetime]) -> Optional[str]:
    if t is None:
        return None
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _from_iso(s: Optional[str]) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        txt = s.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(txt)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except Exception:
        return None


@dataclass
class OracleState:
    """household_id -> first instant it was observed as a declared payer."""

    path: Path
    first_declared_at: Dict[str, dt.datetime] = field(default_factory=dict)
    dirty: bool = False

    @classmethod
    def load(cls, path: Path) -> "OracleState":
        """A missing OR corrupt file yields empty state, never an error --
        see the module docstring for why losing this file is safe."""
        st = cls(path=Path(path))
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return st
        if not isinstance(raw, dict):
            return st
        rows = raw.get("first_declared_at")
        if isinstance(rows, dict):
            for hid, iso in rows.items():
                t = _from_iso(iso) if isinstance(iso, str) else None
                if isinstance(hid, str) and hid and t is not None:
                    st.first_declared_at[hid] = t
        return st

    def save(self) -> None:
        """Atomic write, same convention as every other state file here."""
        payload = {
            "schema": SCHEMA,
            "first_declared_at": {h: _to_iso(t)
                                  for h, t in sorted(self.first_declared_at.items())},
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)
        self.dirty = False

    def observe(self, household_ids: Iterable[str],
               now: Optional[dt.datetime] = None) -> Dict[str, dt.datetime]:
        """Record `now` for every id not already known.

        Returns the full first_declared_at map for exactly the ids passed in
        (pre-existing entries included), which is what a caller assembling
        payer_oracle.DeclaredPayer rows needs in one call.

        A household that STOPS being a declared payer (billing removed, or
        `exempt` flips true) keeps its row rather than being pruned. Deleting
        it would restart its clock the moment it becomes a declared payer
        again, silently reopening a settle window for a household that has
        been declared for months -- exactly the kind of quiet re-arming a
        settle window exists to prevent.
        """
        now = now or utcnow()
        ids = list(household_ids)
        for hid in ids:
            if hid not in self.first_declared_at:
                self.first_declared_at[hid] = now
                self.dirty = True
        return {hid: self.first_declared_at[hid] for hid in ids
                if hid in self.first_declared_at}


def default_state_path() -> Path:
    base = os.environ.get("QFLIX_ENTITLEMENT_STATE_DIR")
    if base:
        return Path(base) / "oracle-state.json"
    return Path.home() / ".opt" / "maint" / "entitlement" / "oracle-state.json"

"""lib/audit/detectors — one module per enrolled defect class.

Discovery is dynamic ON PURPOSE. A hand-maintained import list would let a
detector exist without ever being loaded, which is the same silent-omission
shape the whole regime exists to kill. Instead every `cNN_*.py` in this package
is discovered, and the bijection meta-check demands that each one is claimed by
exactly one class in manifest/defect-classes.yaml.

A detector module contract:
    NAME       str   module id, matching `detector:` in the ledger
    CLASS_ID   str   the defect class it implements
    BOUNDARY   str   one line naming the enumeration boundary
    detect(ctx) -> DetectorResult

`detect` must emit a verdict for EVERY instance in its boundary, including the
`ok` ones. Emitting only the bad ones would make "how much did you look at?"
unanswerable, which is defect (b) — no coverage ledger — reintroduced inside
the fix for defect (b).
"""
from __future__ import annotations

import importlib
import pkgutil
import re
from types import ModuleType
from typing import Dict, List

from ..model import RegimeError

_NAME_RX = re.compile(r"^c\d{2}_[a-z0-9_]+$")


def available() -> List[str]:
    """Sorted module ids of every detector in this package."""
    return sorted(
        name for _finder, name, ispkg in pkgutil.iter_modules(__path__)
        if not ispkg and _NAME_RX.match(name)
    )


_CACHE: Dict[str, ModuleType] = {}


def load(name: str) -> ModuleType:
    mod = _CACHE.get(name)
    if mod is not None:
        return mod
    try:
        mod = importlib.import_module(__name__ + "." + name)
    except Exception as exc:  # noqa: BLE001 - a broken detector is a REGIME failure
        raise RegimeError("detector " + name + " failed to import: " + repr(exc)) from exc
    for attr in ("NAME", "CLASS_ID", "BOUNDARY", "detect"):
        if not hasattr(mod, attr):
            raise RegimeError("detector " + name + " is missing required attribute " + attr)
    if mod.NAME != name:
        raise RegimeError("detector " + name + " declares NAME=" + repr(mod.NAME))
    _CACHE[name] = mod
    return mod

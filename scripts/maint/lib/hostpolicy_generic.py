"""lib/hostpolicy_generic.py -- a plain Linux host (spec 5.6).

Deliberately knows nothing about panel tooling or a docker gateway. Window,
port range and upgrade list come from operator config or are empty.

Optional config (secrets dir, plain text):
  host.windows      one window per line: "<weekday 0=Mon..6> <start>-<end>"
                    hours UTC, end exclusive, e.g. "6 2-4". Absent = no window.
  host.port-range   "<lo>-<hi>" candidate port range. Absent = none.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple


def _base():
    key = "_qflix_hp_hostpolicy"
    if key in sys.modules:
        return sys.modules[key]
    path = Path(__file__).resolve().parent / "hostpolicy.py"
    spec = importlib.util.spec_from_file_location(key, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[key] = mod
    spec.loader.exec_module(mod)
    return mod


class GenericPolicy(_base().HostPolicy):
    name = "generic"

    def detect(self) -> bool:
        # "Not the other profile". A shared-slot box carrying profile=generic
        # is a wrong-secret mistake and must fail the preflight.
        return not _base()._sibling("hostpolicy_ultra").UltraPolicy().detect()

    def windows(self) -> List[Tuple[int, int, int]]:
        out: List[Tuple[int, int, int]] = []
        text = _base().read_secret("host.windows") or ""
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                dow, span = line.split()
                lo, hi = span.split("-")
                d, s, e = int(dow), int(lo), int(hi)
            except ValueError:
                continue                     # malformed line: ignore, not crash
            if 0 <= d <= 6 and 0 <= s < e <= 24:
                out.append((d, s, e))
        return out

    def gate_probe(self) -> Optional[str]:
        return None

    def port_candidates(self) -> Sequence[int]:
        text = _base().read_secret("host.port-range") or ""
        try:
            lo, hi = (int(x) for x in text.split("-"))
        except ValueError:
            return []
        return list(range(lo, hi + 1)) if 0 < lo <= hi <= 65535 else []

    def proxy_reload(self) -> Optional[List[str]]:
        return None

    def quota(self) -> str:
        return "statvfs"

    def task_ceiling(self) -> Optional[int]:
        try:
            import resource
            soft = resource.getrlimit(resource.RLIMIT_NPROC)[0]
        except (ImportError, OSError, ValueError, AttributeError):
            return None
        return None if soft == resource.RLIM_INFINITY else int(soft)

    def docker_gateway(self) -> Optional[str]:
        return None

    def upgrade_sweeps(self) -> List[str]:
        return ["native"]

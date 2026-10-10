"""lib/hostpolicy_ultra.py -- everything Ultra.cc-specific lives here (spec 5.6).

Loaded by hostpolicy.py by file path. No other module may hard-code the
Monday window, the docker gateway or the app-* panel tools; ask the policy.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
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


class UltraPolicy(_base().HostPolicy):
    name = "ultra"

    def detect(self) -> bool:
        # The panel tool exists only on an Ultra.cc slot. It is a CROSS-CHECK
        # for the explicit secret, never the source of the profile.
        return shutil.which("app-ports") is not None

    def windows(self) -> List[Tuple[int, int, int]]:
        # Mon 11:00-14:59 UTC. Matches manitoba-maint-window.timer (opens
        # 11:00) and the watchdog timer (15:00).
        return [(0, 11, 15)]

    def gate_probe(self) -> Optional[str]:
        return "plex"          # I-9: the UCC gate is probed through plex only

    def port_candidates(self) -> Sequence[int]:
        try:
            out = subprocess.run(["app-ports", "free"], capture_output=True,
                                 text=True, timeout=30, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        cands = [int(t) for t in out.split() if t.isdigit()]
        if not cands:
            # Empty is a visible refusal downstream (ports.claim raises); say why.
            print("hostpolicy_ultra: app-ports free yielded no candidates",
                  file=sys.stderr)
        return cands

    def proxy_reload(self) -> Optional[List[str]]:
        return ["app-nginx", "reload"]

    def quota(self) -> str:
        return "scripts/canaries/quota.sh"      # wraps `quota -p`

    def task_ceiling(self) -> Optional[int]:
        return 2000

    def docker_gateway(self) -> Optional[str]:
        return "172.17.0.1"

    def upgrade_sweeps(self) -> List[str]:
        return ["app-upgrade-all"]

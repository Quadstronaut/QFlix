"""C-12 raw-host-literal ratchet (shrink-only).

QFLX-23 (spec F-16). Two things tie a script to one hosting provider:
  * the docker-gateway address, hard-coded where a container must reach a
    host app; it now comes from the `net.app_host` secret (or the manifest's
    `hostname_ref`);
  * the panel tools `app-<x>` / `app-ports` / `app-nginx` called raw; they go
    through ~/bin/appctl or lib/hostpolicy*.py.

What is left of each is baselined PER FILE in manifest/raw-host-allowlist.yaml
(keys: app_calls, host_literals). The allowlist may only shrink:
  new-literal           a literal in a file with no allowlist entry
  literal-grew          more literals than the entry allows
  allowlist-not-shrunk  fewer literals than the entry allows (or the file is
                        gone): tighten the entry, so a removed literal cannot
                        be re-added later under its old budget

Only executable lines count: comments and Python docstrings are prose. The
policy homes (lib/hostpolicy*.py, scripts/lib/appctl) are the one legitimate
place for the literals; REA's log-fingerprint text (scripts/local-llm) is data,
not a call, and is out of the scan.

ENFORCED from the day it lands: the baseline IS the current state.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

import yaml

from ..model import FINDING, OK, DetectorResult, RegimeError, Verdict

NAME = "c12_raw_host_literals"
CLASS_ID = "C-12"
BOUNDARY = "executable lines of tracked scripts and manifests: raw app-<x> calls and the docker-gateway literal"

ALLOWLIST_PATH = "manifest/raw-host-allowlist.yaml"

SCAN_GLOBS = [
    "scripts/**/*.sh",
    "scripts/**/*.py",
    "scripts/data/*.tmpl",
    "manifest/*.yaml",
]
EXCLUDE_GLOBS = [
    "scripts/local-llm/**",          # REA fingerprint text (data, not a call)
]
POLICY_HOMES = (
    "scripts/maint/lib/hostpolicy.py",
    "scripts/maint/lib/hostpolicy_ultra.py",
    "scripts/maint/lib/hostpolicy_generic.py",
)

# The gateway literal, assembled so this module is not its own hit.
HOST_LITERAL = re.compile(r"172\.17\.0" + r"\.1\b")
# A raw panel call: `app-` + a name, not preceded by a word char or one of
# $ . / { } < > - (variable names, paths, flags like --app-ports).
APP_CALL = re.compile(r"(?<![\w$./{}<>-])app-[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def code_lines(path: str, text: str) -> List[str]:
    """Executable lines: drops `#` comment lines, and for Python also the
    bodies of triple-quoted docstrings."""
    out: List[str] = []
    in_doc = False
    is_py = path.endswith(".py")
    for line in text.split("\n"):
        s = line.strip()
        if is_py:
            n = line.count('"""') + line.count("'''")
            if in_doc:
                if n % 2 == 1:
                    in_doc = False
                continue
            if n % 2 == 1:
                in_doc = True
                continue
            if n >= 2 and (s.startswith('"""') or s.startswith("'''")):
                continue            # one-line docstring
        if s.startswith("#"):
            continue
        out.append(line)
    return out


def count_literals(path: str, text: str) -> Tuple[int, int]:
    """(app_calls, host_literals) on executable lines."""
    app = host = 0
    for line in code_lines(path, text):
        app += len(APP_CALL.findall(line))
        host += len(HOST_LITERAL.findall(line))
    return app, host


def _load_allowlist(repo) -> Dict[str, Dict[str, int]]:
    text = repo.read_optional(ALLOWLIST_PATH)
    if text is None:
        raise RegimeError(ALLOWLIST_PATH + " is missing; the ratchet has no baseline")
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RegimeError(ALLOWLIST_PATH + " is not valid YAML: " + str(exc)) from exc
    files = data.get("files")
    if not isinstance(files, dict):
        raise RegimeError(ALLOWLIST_PATH + " needs a `files:` mapping")
    out: Dict[str, Dict[str, int]] = {}
    for path, entry in files.items():
        entry = entry or {}
        out[str(path)] = {"app_calls": int(entry.get("app_calls", 0)),
                          "host_literals": int(entry.get("host_literals", 0))}
    return out


def detect(ctx) -> DetectorResult:
    repo = ctx.repo
    allow = _load_allowlist(repo)
    from ..repo import glob_match

    files = [p for p in repo.tracked_matching(SCAN_GLOBS)
             if p not in POLICY_HOMES
             and not any(glob_match(g, p) for g in EXCLUDE_GLOBS)]
    tracked = set(repo.tracked)
    verdicts: List[Verdict] = []

    for path in files:
        app, host = count_literals(path, repo.read(path))
        want = allow.get(path)
        iid = path + ":raw-host"
        if want is None:
            if app or host:
                verdicts.append(Verdict(
                    iid, "new-literal", FINDING, path, 0,
                    "raw app-<x> call or gateway literal in a file with no allowlist entry; "
                    "use appctl / hostpolicy / the net.app_host secret"))
            else:
                verdicts.append(Verdict(iid, "clean", OK, path, 0, "no raw literal"))
            continue
        if app > want["app_calls"] or host > want["host_literals"]:
            verdicts.append(Verdict(
                iid, "literal-grew", FINDING, path, 0,
                "raw literals exceed the allowlist (app calls %d/%d, host literals %d/%d); "
                "the allowlist may only shrink" % (app, want["app_calls"], host, want["host_literals"])))
        elif app < want["app_calls"] or host < want["host_literals"]:
            verdicts.append(Verdict(
                iid, "allowlist-not-shrunk", FINDING, path, 0,
                "fewer raw literals than allowed (app calls %d/%d, host literals %d/%d); "
                "tighten the allowlist entry" % (app, want["app_calls"], host, want["host_literals"])))
        else:
            verdicts.append(Verdict(iid, "baselined", OK, path, 0,
                                    "at its allowlist budget"))

    scanned = set(files)
    for path in sorted(allow):
        if path in scanned:
            continue
        why = "no longer tracked" if path not in tracked else "no longer in scan scope"
        verdicts.append(Verdict(
            path + ":raw-host", "allowlist-not-shrunk", FINDING, path, 0,
            "allowlist entry for a file that is " + why + "; delete the entry"))

    return DetectorResult(
        boundary_name=BOUNDARY,
        boundary_size=len(files),
        verdicts=verdicts,
        metrics={"files_scanned": len(files), "allowlisted_files": len(allow)},
    )

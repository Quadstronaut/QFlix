#!/usr/bin/env python3
"""lib/unpackerr_conf.py - validate an unpackerr.conf (QFLX-46).

THE TRAP: unpackerr's general settings (log_file, interval, ...) are TOP-LEVEL
TOML keys. app-unpackerr's panel regenerate wraps them in a `[[general]]`
array-table header; unpackerr then ignores every one, log_file included, and
the durable log goes dark with no error (2026-08-26, again 2026-10-05).

check(text) returns a list of problems (empty = good):
  general-header       a [general] / [[general]] header exists
  log-file-missing     no top-level log_file key (before the first table)
  unresolved-placeholder  a {{X}} survived rendering
  toml-invalid         tomllib (3.11+) rejects the text
Pure stdlib, no lib.* imports (lib/ is a merged namespace package; never add
an __init__.py).

CLI: unpackerr_conf.py check FILE   -> prints problems, exit 1 if any.
"""
from __future__ import annotations

import re
import sys

_GENERAL = re.compile(r"^\s*\[\[?\s*general\s*\]\]?\s*(#.*)?$", re.I)
_TABLE = re.compile(r"^\s*\[")
_EMPTY_KEY = re.compile(r"""^\s*api_key\s*=\s*(""|'')\s*$""")
_EMPTY_URL = re.compile(r"""^\s*url\s*=\s*"http://:""")
_LOGFILE =re.compile(r"""^\s*log_file\s*=\s*["'][^"']+["']""")


def check(text: str) -> list[str]:
    problems: list[str] = []
    lines = text.splitlines()
    if any(_GENERAL.match(ln) for ln in lines):
        problems.append("general-header")
    top_log = False
    for ln in lines:
        if _TABLE.match(ln):
            break                      # keys past here belong to a table
        if _LOGFILE.match(ln):
            top_log = True
    if not top_log:
        problems.append("log-file-missing")
    if any("{{" in ln for ln in lines if not ln.lstrip().startswith("#")):
        problems.append("unresolved-placeholder")
    # An empty secret renders url = "http://:/" / api_key = "": unpackerr then
    # sees 0 servers and shuts down. Caught live 2026-10-10.
    for ln in lines:
        if _EMPTY_KEY.match(ln) or _EMPTY_URL.match(ln):
            problems.append("empty-secret")
            break
    try:
        import tomllib
    except ImportError:                # box python < 3.11: line scan only
        tomllib = None
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
            if "general" in data and "general-header" not in problems:
                problems.append("general-header")
            if "log_file" not in data and "log-file-missing" not in problems:
                problems.append("log-file-missing")
        except Exception:
            problems.append("toml-invalid")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "check":
        sys.stderr.write("usage: unpackerr_conf.py check FILE\n")
        return 64
    with open(argv[2], encoding="utf-8") as fh:
        problems = check(fh.read())
    for p in problems:
        print(p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

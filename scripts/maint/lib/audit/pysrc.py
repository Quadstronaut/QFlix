"""lib/audit/pysrc.py — the shared Python-source boundary and AST helpers.

Every AST detector uses the SAME file set. Defining it once is not tidiness: if
C-02 and C-03 disagreed about which files count, their "we enumerated 100% of
the boundary" claims would be about different boundaries and the coverage
numbers would not compose.
"""
from __future__ import annotations

import ast
from typing import Dict, Iterator, List, Optional, Tuple

from .model import RegimeError

PY_GLOB = "scripts/**/*.py"


def is_test_path(path: str) -> bool:
    """Test code is excluded from the code-quality classes.

    Rationale, stated so it can be argued with: a bare `except Exception: pass`
    in a fixture is a different animal from one in a janitor that deletes
    files. Tests are still in scope for C-10 (are they in CI?) and C-08 (do
    they name a decommissioned component?), which is where they matter.
    """
    return "/tests/" in path or path.rsplit("/", 1)[-1].startswith("test_")


def source_files(repo) -> List[str]:
    return [p for p in repo.tracked_matching([PY_GLOB]) if not is_test_path(p)]


def parse(repo, path: str) -> Optional[ast.AST]:
    """Parse a tracked file, or None if it will not parse.

    A syntax error in a tracked .py is itself worth knowing about, so callers
    count the failures rather than silently skipping them — the auditor must
    never quietly shrink its own boundary.
    """
    try:
        return ast.parse(repo.read(path), filename=path)
    except SyntaxError:
        return None
    except RegimeError:
        raise


def parent_map(tree: ast.AST) -> Dict[int, ast.AST]:
    """id(node) -> parent node. ast gives no parent pointers and several
    detectors need to ask "what encloses this call?"."""
    parents: Dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def enclosing_function(node: ast.AST, parents: Dict[int, ast.AST]) -> Optional[ast.AST]:
    cur = parents.get(id(node))
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur
        cur = parents.get(id(cur))
    return None


def walk_with_scope(tree: ast.AST) -> Iterator[Tuple[ast.AST, Optional[ast.AST]]]:
    parents = parent_map(tree)
    for node in ast.walk(tree):
        yield node, enclosing_function(node, parents)

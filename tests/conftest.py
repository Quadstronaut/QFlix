import sys
import os

_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.dirname(_HERE)

# Each path below contributes its own `lib/` directory to the namespace package
# `lib`. Adding all of them lets `from lib.<module>` resolve to whichever dir
# physically owns <module>. Collisions are avoided by renaming: e.g. the
# seedbox-MCP-side reader of ~/.opt/maint/state.json lives at `lib/maint_state.py`
# (not `lib/state.py`) so it doesn't shadow `scripts/maint/lib/state.py`.
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "maint"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "qflix-newsletter"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "mcp"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "local", "qflix-mcp"))

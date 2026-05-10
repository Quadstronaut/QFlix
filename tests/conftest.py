import sys
import os

_HERE = os.path.dirname(__file__)

# Allow `from lib.manifest import ...` in unit tests (manitoba-maint codebase)
sys.path.insert(0, os.path.join(_HERE, "..", "scripts", "maint"))

# Allow `from qflix_newsletter import ...` in unit tests
sys.path.insert(0, os.path.join(_HERE, "..", "scripts", "qflix-newsletter"))

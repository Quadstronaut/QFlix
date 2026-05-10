import sys
import os

# Allow `from lib.manifest import ...` in unit tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts", "maint"))

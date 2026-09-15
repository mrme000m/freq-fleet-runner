"""pytest path setup for grid/tests — put the repo root (parent of grid/)
on sys.path so `import grid.*` resolves; tests themselves are pure stdlib."""
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

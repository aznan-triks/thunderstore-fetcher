"""Pytest bootstrap.

Makes the repository importable no matter how pytest is invoked and redirects
the app's relative "./data" default into a throwaway directory so test runs
never touch (or create) a real data/ folder inside the repository.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="thunderstore-fetcher-tests-")
os.chdir(_TMP)

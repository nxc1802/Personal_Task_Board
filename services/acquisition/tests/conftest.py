"""Pytest configuration for acquisition tests."""

from pathlib import Path
import sys

# Ensure repository root is on sys.path for test doubles import
ROOT_DIR = Path(__file__).resolve().parent.parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

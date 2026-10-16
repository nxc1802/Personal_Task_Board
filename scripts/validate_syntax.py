"""Pre-commit syntax validation script for Personal Task Board."""
import py_compile
import sys
from pathlib import Path

def main():
    has_errors = False
    root = Path(__file__).resolve().parent.parent

    for py_file in root.glob("**/*.py"):
        # Ignore virtualenvs, data, caches, and test artifacts
        if any(ignored in str(py_file) for ignored in [".venv", "venv", "__pycache__", ".pytest_cache", "data"]):
            continue
        try:
            py_compile.compile(str(py_file), doraise=True)
        except Exception as e:
            print(f"Syntax error in {py_file}: {e}", file=sys.stderr)
            has_errors = True

    if has_errors:
        sys.exit(1)
    print("Pre-commit syntax validation passed!")

if __name__ == "__main__":
    main()

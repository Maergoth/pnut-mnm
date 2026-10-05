"""Module entry point: ``python -m mnmparse.app [-v] [--selftest-seconds N] [--config PATH]``.

Uses an absolute import so the same file also works as the PyInstaller entry script.
"""

from __future__ import annotations

import sys

from mnmparse.app.main import run

if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))

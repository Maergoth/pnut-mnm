"""Module entry point: ``python -m mnmparse.app [-v] [--selftest-seconds N] [--config PATH]``.

Uses an absolute import so the same file also works as the PyInstaller entry script.
"""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch diagnostics before importing Qt, so DLL failures become reports."""
    args = list(sys.argv[1:] if argv is None else argv)
    if "--smoke-test" in args:
        import argparse
        from pathlib import Path

        from mnmparse.app.smoke import run_smoke_test

        parser = argparse.ArgumentParser(description="Check the packaged application without starting capture")
        parser.add_argument("--smoke-test", action="store_true", required=True)
        parser.add_argument("--report", type=Path, required=True)
        parsed = parser.parse_args(args)
        return run_smoke_test(parsed.report)

    from mnmparse.app.main import run

    return run(args)

if __name__ == "__main__":
    sys.exit(main())

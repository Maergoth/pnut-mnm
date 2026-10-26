"""Module entry point: ``python -m mnmparse.app [-v] [--selftest-seconds N] [--config PATH]``.

Uses an absolute import so the same file also works as the PyInstaller entry script.
"""

from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch diagnostics before importing Qt, so DLL failures become reports."""
    args = list(sys.argv[1:] if argv is None else argv)
    from mnmparse.app import APP_VERSION
    from mnmparse.app.crash_diagnostics import enable_startup_diagnostics

    enable_startup_diagnostics(args, version=APP_VERSION)
    if "--atlas-smoke" in args:
        import argparse
        from pathlib import Path
        from mnmparse.app.atlas_smoke import run_native_smoke

        parser = argparse.ArgumentParser(description="Check the shared Windows web renderer offline")
        parser.add_argument("--atlas-smoke", action="store_true", required=True)
        parser.add_argument("--report", type=Path, required=True)
        parser.add_argument("--storage", type=Path, required=True)
        parsed = parser.parse_args(args)
        return run_native_smoke(parsed.report, parsed.storage)
    from mnmparse.app.launch_identity import LaunchError, launch_if_needed, report_launch_error

    try:
        launched = launch_if_needed(args, wait="--smoke-test" in args)
    except LaunchError as exc:
        report_launch_error(exc, args)
        return 1
    if launched is not None:
        return launched
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

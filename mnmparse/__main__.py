"""Module entry point so the tool can be run as ``python -m mnmparse <cmd>``."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())

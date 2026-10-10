"""Write build metadata immediately before freezing the application."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mnmparse import __version__
from mnmparse.storage import atomic_json


def main():
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dirty = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip())
    info = {"schema": 1, "version": __version__, "commit": commit, "dirty": dirty,
            "built_at": datetime.now(timezone.utc).isoformat()}
    atomic_json(ROOT / "build" / "build-info.json", info)
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

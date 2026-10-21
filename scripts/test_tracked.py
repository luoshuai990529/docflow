"""Run only Python test modules committed to the Git index."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def tracked_test_modules() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", "tests"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    modules = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8"))
        if path.suffix == ".py" and path.name.startswith("test_"):
            modules.append(".".join(path.with_suffix("").parts))
    return sorted(modules)


def main() -> int:
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "server" / "src"))
    modules = tracked_test_modules()
    if not modules:
        print("No tracked Python test modules were found.", file=sys.stderr)
        return 2

    loader = unittest.TestLoader()
    suite = unittest.TestSuite(loader.loadTestsFromName(module) for module in modules)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

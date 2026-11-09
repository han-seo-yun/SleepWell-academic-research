"""Run the full unittest suite and preserve the exact runner output."""
from __future__ import annotations

import importlib.metadata
import platform
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "deliverables" / "validation" / "test-results.txt"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for stream in self.streams:
            stream.write(text)
            stream.flush()

    def flush(self):
        for stream in self.streams:
            stream.flush()


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    packages = ["numpy", "pandas", "scipy", "scikit-learn", "matplotlib", "openpyxl"]
    header = [
        "Research Workbench validation",
        f"Python: {platform.python_version()} ({sys.executable})",
        f"Platform: {platform.platform()}",
        "Packages: " + ", ".join(f"{name}={importlib.metadata.version(name)}" for name in packages),
        "Command equivalent: python -m unittest discover -s tests -v",
        "",
    ]
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as log:
        tee = Tee(sys.stdout, log)
        tee.write("\n".join(header))
        suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), pattern="test*.py")
        result = unittest.TextTestRunner(stream=tee, verbosity=2).run(suite)
    print(f"Validation log: {OUTPUT}")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

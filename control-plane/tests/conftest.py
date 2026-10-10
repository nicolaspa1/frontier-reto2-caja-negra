"""Import-path setup: harness/ and experiments/ import each other by bare module name."""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harness")
EXPERIMENTS = os.path.join(ROOT, "experiments")

for _p in (HARNESS, EXPERIMENTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def pytest_report_header(config):
    return f"harness root: {ROOT}"

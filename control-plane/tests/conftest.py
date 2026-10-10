"""Shared pytest fixtures and import-path setup for the harness test suite.

The harness modules live in ``harness/`` and ``experiments/`` and import each
other by bare module name (``import compare``, ``import cn`` ...). We add both
directories to ``sys.path`` so the tests can import them the same way the
scripts do when run from their own folder.
"""
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

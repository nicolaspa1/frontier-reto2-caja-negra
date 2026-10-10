"""Run fit.py --selftest and templates.py --selftest as subprocesses, the way an operator would.
(genspec.py --check is covered read-only by test_genspec.py.)
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harness")


def run(*args):
    return subprocess.run(
        [sys.executable, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_fit_selftest_passes():
    proc = run(os.path.join(HARNESS, "fit.py"), "--selftest")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SELFTEST" in proc.stdout


def test_templates_selftest_passes():
    proc = run(os.path.join(HARNESS, "templates.py"), "--selftest")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SELFTEST" in proc.stdout

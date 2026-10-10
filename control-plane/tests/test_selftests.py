"""Run the built-in self-tests of the offline tools as subprocesses.

These invoke the real CLIs the same way an operator would:
  * fit.py --selftest       (regression of the control-algorithm search)
  * templates.py --selftest (synthetic-log diff/patch regression)

(genspec.py --check is covered by test_genspec.py, which runs it read-only
against a frozen snapshot of the log so the live monitor cannot make it flaky.)
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

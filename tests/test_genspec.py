"""Read-only test of `genspec.py --check`.

genspec reads a hardcoded log path and (even under --check) rewrites the
output document, so this test:
  * points genspec.OUT at a temp file (never touching spec/especificacion.md),
  * feeds it a FROZEN snapshot of experiments/log.jsonl taken from the git
    HEAD commit, which is the log the committed especificacion.md was built
    from. This keeps the check deterministic even though a live monitor keeps
    appending to the working-tree log.

If git or the HEAD snapshot is unavailable the test skips rather than giving a
misleading pass/fail.
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "harness"))


def _head_log_snapshot(tmp_path):
    try:
        out = subprocess.run(
            ["git", "show", "HEAD:experiments/log.jsonl"],
            cwd=ROOT, capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout:
        return None
    snap = tmp_path / "log_head.jsonl"
    snap.write_bytes(out.stdout)
    return str(snap)


def test_genspec_check_passes_on_frozen_log(tmp_path, monkeypatch):
    snapshot = _head_log_snapshot(tmp_path)
    if snapshot is None:
        pytest.skip("git HEAD snapshot of the log not available")

    import genspec

    out_path = str(tmp_path / "especificacion.md")
    monkeypatch.setattr(genspec, "LOG", snapshot)
    monkeypatch.setattr(genspec, "OUT", out_path)
    monkeypatch.setattr("sys.argv", ["genspec", "--check", "--out", out_path])

    with pytest.raises(SystemExit) as exc:
        genspec.main()
    assert exc.value.code == 0

    # it still produced a plausible document at the temp path (read-only wrt repo)
    assert os.path.exists(out_path)
    text = open(out_path, encoding="utf-8").read()
    assert "# Especificaci" in text  # the spec title (Spanish deliverable text)

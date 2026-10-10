"""Read-only `genspec.py --check`: OUT is redirected to a temp file and the input is the frozen
log snapshot from git HEAD (the one the committed especificacion.md was built from), so a
live monitor appending to the working-tree log cannot make it flaky. Skips without git.
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
            ["git", "show", "HEAD:control-plane/experiments/log.jsonl"],
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

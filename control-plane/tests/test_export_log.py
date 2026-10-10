"""Test export_log.py against a tiny synthetic log (no real files touched)."""
import json
import os

import export_log


def _write_log(path):
    records = [
        {
            "n": 1,
            "section": "replay-ejemplos-oficiales",
            "req": {"method": "POST", "path": "/msg", "body": {"m": "PING;64"}},
            "res": {"status": 200, "body": '{"pong":true}', "x_req": "abc123"},
        },
        {
            "n": 2,
            "section": "replay-ejemplos-oficiales",
            "req": {"method": "POST", "path": "/__reset", "body": None},
            "res": {"status": 200, "body": '{"ok":true}', "x_req": "def456"},
        },
        {
            "n": 3,
            "section": "single:GET /health",
            "req": {"method": "GET", "path": "/health", "body": None,
                    "auth_kind": "valid"},
            "res": {"status": 200, "body": '{"ok":true}', "x_req": None},
        },
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_export_produces_official_format(tmp_path, monkeypatch):
    log_path = str(tmp_path / "log.jsonl")
    hip_path = str(tmp_path / "hipotesis.json")
    out_path = str(tmp_path / "experimentos.jsonl")
    _write_log(log_path)

    monkeypatch.setattr(export_log, "LOG", log_path)
    monkeypatch.setattr(export_log, "HIP_MAP", hip_path)
    monkeypatch.setattr(export_log, "OUT", out_path)
    monkeypatch.setattr("sys.argv", ["export_log"])

    export_log.main()

    rows = [json.loads(l) for l in open(out_path, encoding="utf-8")]
    assert len(rows) == 3

    expected_keys = {"id", "seq", "x_req", "hipotesis", "request",
                     "status", "response", "conclusion"}
    for row in rows:
        assert set(row.keys()) == expected_keys

    assert rows[0]["request"] == {"m": "PING;64"}
    assert rows[0]["status"] == 200
    assert rows[0]["response"] == {"pong": True}
    assert rows[0]["x_req"] == "abc123"

    assert rows[1]["request"] == {"metodo": "POST", "ruta": "/__reset"}

    # the official `request` is only the body sent: no injected "auth" key
    # (the credential used, when not the valid token, goes to `conclusion`)
    assert "auth" not in rows[2]["request"]

    # id is the export line number (unique, 1-based), x_req numeric when it is
    assert [r["id"] for r in rows] == [1, 2, 3]
    assert rows[1]["x_req"] == "def456"

    # conclusion is always filled with a short sentence derived from the response
    assert all(r["conclusion"] for r in rows)
    assert rows[0]["conclusion"] == "200 pong"

    # the hypothesis map was created at the patched path
    assert os.path.exists(hip_path)
    hmap = json.load(open(hip_path, encoding="utf-8"))
    assert "replay-ejemplos-oficiales" in hmap


def test_print_map_only(tmp_path, monkeypatch, capsys):
    log_path = str(tmp_path / "log.jsonl")
    hip_path = str(tmp_path / "hipotesis.json")
    _write_log(log_path)
    monkeypatch.setattr(export_log, "LOG", log_path)
    monkeypatch.setattr(export_log, "HIP_MAP", hip_path)
    monkeypatch.setattr("sys.argv", ["export_log", "--print-map"])

    export_log.main()
    out = capsys.readouterr().out
    assert "replay-ejemplos-oficiales" in out

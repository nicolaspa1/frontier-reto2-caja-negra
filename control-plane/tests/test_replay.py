"""Test replay.py end to end against a local HTTP stub (no network).

The stub echoes recorded answers: POST /__reset returns {"ok":true} and
POST /msg returns the status/body recorded for that exact message.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import record
import replay


RECORDED = {
    "PING;64": (200, '{"pong":true}'),
    "ALTA;Lucia;82": (201, '{"cuentaId":"AC-0001"}'),
    "ALTA;Mateo;3": (201, '{"cuentaId":"AC-0002"}'),
    "CONSULTA;AC-0001;14": (200, '{"cuenta_id":"AC-0001","saldo":0}'),
    "SALDOS;AC-0001;38": (400, '{"error":"E301","mensaje":"comando desconocido "}'),
}


def make_handler(answers):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _send(self, status, body):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b""
            if self.path == "/__reset":
                self._send(200, '{"ok":true}')
                return
            if self.path == "/msg":
                try:
                    m = json.loads(raw.decode())["m"]
                except Exception:
                    self._send(400, '{"error":"E100"}')
                    return
                status, body = answers.get(m, (500, '{"error":"unknown"}'))
                self._send(status, body)
                return
            self._send(404, '{"error":"E404"}')

    return Handler


@pytest.fixture
def stub():
    """Start a stub server echoing RECORDED; yield its base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(dict(RECORDED)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()


def _case():
    steps = [{"m": m, "status": s, "body": b} for m, (s, b) in RECORDED.items()]
    return [{"case_id": "stub-1", "steps": steps}]


def test_replay_all_steps_match_stub(stub):
    transport = replay.HttpxTransport(stub, token="lab")
    res = replay.run(_case(), transport, max_diffs=20)
    assert res["total_steps"] == len(RECORDED)
    assert res["equal_steps"] == res["total_steps"]
    assert res["case_pass"] == 1
    assert not res["diffs"]


def test_replay_detects_a_diff(stub):
    # Corrupt one recorded answer so replay reports a difference.
    case = _case()
    case[0]["steps"][0]["body"] = '{"pong":false}'  # stub still returns true
    transport = replay.HttpxTransport(stub, token="lab")
    res = replay.run(case, transport, max_diffs=20)
    assert res["equal_steps"] == res["total_steps"] - 1
    assert res["case_pass"] == 0
    assert res["diffs"]
    cid, i, cmd, reason = res["diffs"][0]
    assert cid == "stub-1"
    assert cmd == "PING"


def test_coverage_counts_errors_and_commands(stub):
    transport = replay.HttpxTransport(stub, token="lab")
    res = replay.run(_case(), transport, max_diffs=20)
    assert res["cov_cmds"].get("PING") == 1
    assert res["cov_cmds"].get("ALTA") == 2
    assert res["cov_errors"].get("E301") == 1


def test_err_of_and_cmd_of_helpers():
    assert replay.err_of('{"error":"E401"}') == "E401"
    assert replay.err_of('{"ok":true}') is None
    assert replay.cmd_of({"m": "DEPOSITO;AC-0001;100"}) == "DEPOSITO"
    assert replay.cmd_of({"raw": {"method": "GET", "path": "/health"}}) == "RAW GET /health"


def test_diff_line_format_is_parsed_by_record(tmp_path):
    """Guard the cross-script contract: record.parse_diff_report must extract
    the case_id from a line printed by replay.main in its exact format."""
    cid, i, cmd, reason = "single-07", 2, "ALTA", "body differs"
    line = f"   {cid} paso {i} [{cmd}]: {reason}\n"
    report = tmp_path / "diffreport.txt"
    report.write_text(line, encoding="utf-8")
    ids = record.parse_diff_report(str(report))
    assert ids == {"single-07"}


def test_is_original_matches_netloc(monkeypatch):
    monkeypatch.setattr(replay.L, "ORIGINAL_URL", "https://caja-negra.example/api")
    assert replay.is_original("https://caja-negra.example/other")
    assert not replay.is_original("http://localhost:8091")

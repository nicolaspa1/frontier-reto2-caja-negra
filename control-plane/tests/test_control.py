"""The 20 official examples (tests/fixtures/official-examples.jsonl) must be consistent with
control.control / control.message: E201 = not a 1-2 digit control, E202 = well formed but
wrong value, PING = exempt, anything else = the computed control equals the segment.
"""
import json
import os

import control

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "tests", "fixtures", "official-examples.jsonl")


def load_examples():
    rows = []
    with open(EXAMPLES, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def test_examples_file_has_20_rows():
    assert len(load_examples()) == 20


def test_official_examples_all_consistent():
    for ex in load_examples():
        m = ex["request"]["m"]
        payload, segment = m.rsplit(";", 1)
        cmd = payload.split(";")[0].upper()
        resp = ex["response"]
        err = resp.get("error") if isinstance(resp, dict) else None

        if err == "E201":
            # malformed control -> not a 1-2 digit number
            assert not (segment.isdigit() and 1 <= len(segment) <= 2), m
        elif err == "E202":
            # well-formed but wrong value
            assert segment.isdigit(), m
            assert control.control(payload) != int(segment), m
        elif cmd == "PING":
            # control value is not validated for PING
            continue
        else:
            # accepted control (2xx or later business error)
            assert control.control(payload) == int(segment), m
            assert control.message(payload) == f"{payload};{control.control(payload)}"


def test_message_appends_control():
    assert control.message("ALTA;Lucia") == "ALTA;Lucia;82"


def test_known_control_values():
    assert control.control("ALTA;Lucia") == 82
    assert control.control("CONSULTA;AC-0001") == 14


def test_control_is_case_insensitive():
    assert control.control("CONSULTA;AC-0001") == control.control("consulta;ac-0001")


def test_separators_and_non_alnum_discarded():
    # ';' and '-' are discarded, so these payloads share the same control.
    assert control.control("ALTA;Lucia") == control.control("ALTALucia")

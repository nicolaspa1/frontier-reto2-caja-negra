"""Tests for probes.py selection and helpers (no network)."""
import json

import probes


def _sondas_file(tmp_path):
    data = {
        "nombres": [],
        "sondas": [
            {"id": "A1", "ronda_larga": True, "mensajes": ["PING;64"]},
            {"id": "A2", "ronda_larga": True, "no_enviar": True, "mensajes": ["HALT;1"]},
            {"id": "A3", "mensajes": ["ALTA;Lucia;82"]},
            {"id": "A4", "no_enviar": True, "mensajes": ["HALT;2"]},
        ],
    }
    path = tmp_path / "sondas.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_long_round_skips_no_enviar(tmp_path):
    sondas = probes.load_sondas(select_all=False, only=[], sondas_file=_sondas_file(tmp_path))
    ids = [s["id"] for s in sondas]
    assert ids == ["A1"]  # only ronda_larga and not no_enviar


def test_only_subset_skips_no_enviar(tmp_path):
    f = _sondas_file(tmp_path)
    # A2 is explicitly requested but has no_enviar -> excluded
    sondas = probes.load_sondas(select_all=False, only=["A1", "A2", "A3"], sondas_file=f)
    ids = sorted(s["id"] for s in sondas)
    assert ids == ["A1", "A3"]
    assert "A2" not in ids


def test_run_loop_contract_skips_no_enviar(tmp_path):
    # Even with --all (which returns every sonda), the run loop must skip
    # no_enviar probes. We assert the filtering predicate directly.
    f = _sondas_file(tmp_path)
    all_sondas = probes.load_sondas(select_all=True, only=[], sondas_file=f)
    sent = [s["id"] for s in all_sondas if not s.get("no_enviar")]
    assert sent == ["A1", "A3"]


def test_is_infra():
    assert probes.is_infra(503, {}, "")
    assert probes.is_infra(200, {"X-Frontier-Infra": "1"}, "{}")
    assert probes.is_infra(404, {}, "Application not found")
    assert not probes.is_infra(200, {}, '{"ok":true}')


def test_gen_pad_exact_byte_length():
    for n in (64, 128, 512, 8192):
        body = probes.Http._gen_pad(n)
        assert len(body.encode()) == n
        assert json.loads(body)["m"] == "PING;64"


def test_target_label_sanitizes():
    label = probes.target_label("http://localhost:8091")
    assert ":" not in label and "/" not in label

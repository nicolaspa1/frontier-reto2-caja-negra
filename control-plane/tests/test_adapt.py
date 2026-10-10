"""Offline tests for adapt.py's pure pieces: the diff classifier (today's three v2 changes
come out as three groups, a cascade is unknown) and template derivation (2-decimal string
-> ${saldoDecimal}, 1,2,3 counter -> ${intentos}, changed literal verbatim, else unknown).
"""
import json
import os

import adapt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = json.load(open(os.path.join(os.path.dirname(ROOT), "rules", "policy-v1.json"),
                     encoding="utf-8"))


def _diff(case, cmd, m, es, eb, os_, ob):
    return {"case": case, "step": 1, "m": m, "cmd": cmd,
            "exp": {"status": es, "body": eb}, "obs": {"status": os_, "body": ob},
            "exp_err": adapt.R.err_of(eb), "obs_err": adapt.R.err_of(ob)}


def test_classify_v2_three_changes_and_unknown():
    diffs = [
        # transfer that was accepted (200) is now E506 -> numeric limit, group E506
        _diff("c1", "TRANSFER", "TRANSFER;AC-0001;AC-0002;100000;5",
              200, '{"status":"recieved","operacionId":"OP-0001"}',
              422, '{"error":"E506","mensaje":"límite excedido"}'),
        # CONSULTA balance int -> decimal string -> response format, group CONSULTA
        _diff("c2", "CONSULTA", "CONSULTA;AC-0001;14",
              200, '{"cuenta_id":"AC-0001","saldo":250000}',
              200, '{"cuenta_id":"AC-0001","saldo":"2500.00"}'),
        # E510 keeps its code but changes status (409->423) and body -> status+format
        _diff("c3", "TRANSFER", "TRANSFER;AC-0001;AC-0002;100;9",
              409, '{"error":"E510","mensaje":"cuenta bloqueada"}',
              423, '{"error":"E510","mensaje":"cuenta bloqueada","intentos":2}'),
        # cascade (one error turned into a different error): must be unknown
        _diff("c4", "TRANSFER", "TRANSFER;AC-0001;AC-0002;100001;7",
              422, '{"error":"E506","mensaje":"límite excedido"}',
              423, '{"error":"E510","mensaje":"cuenta bloqueada","intentos":1}'),
    ]
    groups, unknown = adapt.classify(diffs, BASE)

    assert set(groups) == {"E506", "CONSULTA", "E510"}
    assert groups["E506"]["classes"] == {"limit"}
    assert groups["CONSULTA"]["classes"] == {"format"}
    assert groups["E510"]["classes"] == {"status+format"}
    # the E506 -> E510 cascade is not a localizable class: reported, not guessed
    assert len(unknown) == 1 and unknown[0]["case"] == "c4"


def test_derive_consulta_saldo_decimal():
    base = BASE["responses"]["CONSULTA"]["body"]          # {"cuenta_id":"${cuentaId}","saldo":${saldo}}
    observed = [{"cuenta_id": "AC-0001", "saldo": "0.00"},
                {"cuenta_id": "AC-0001", "saldo": "1234.56"}]
    expected = [{"cuenta_id": "AC-0001", "saldo": 0},
                {"cuenta_id": "AC-0001", "saldo": 123456}]   # 0/100=0.00, 123456/100=1234.56
    new_body, unknown = adapt.derive_body(base, observed, expected)

    assert not unknown
    assert new_body == '{"cuenta_id":"${cuentaId}","saldo":"${saldoDecimal}"}'
    # ${cuentaId} kept, ${saldo} switched to the string placeholder ${saldoDecimal}
    assert not adapt.tpl_equal(new_body, base)


def test_derive_e510_intentos_counter():
    base = BASE["errors"]["E510"]["body"]                 # {"error":"E510","mensaje":"cuenta bloqueada"}
    observed = [{"error": "E510", "mensaje": "cuenta bloqueada", "intentos": 1},
                {"error": "E510", "mensaje": "cuenta bloqueada", "intentos": 2},
                {"error": "E510", "mensaje": "cuenta bloqueada", "intentos": 3}]
    new_body, unknown = adapt.derive_body(base, observed)

    assert not unknown
    assert new_body == '{"error":"E510","mensaje":"cuenta bloqueada","intentos":${intentos}}'


def test_derive_changed_literal_and_unknown_new_key():
    # changed literal text (E506 mensaje) is taken verbatim, no placeholders
    base = BASE["errors"]["E506"]["body"]                 # {"error":"E506","mensaje":"límite excedido"}
    observed = [{"error": "E506", "mensaje": "limite excedido"}]
    new_body, unknown = adapt.derive_body(base, observed)
    assert new_body == '{"error":"E506","mensaje":"limite excedido"}'
    assert not unknown

    # a new key whose value does NOT increment 1,2,3 is reported as unknown
    observed2 = [{"error": "E506", "mensaje": "límite excedido", "extra": 9},
                 {"error": "E506", "mensaje": "límite excedido", "extra": 9}]
    _, unknown2 = adapt.derive_body(base, observed2)
    assert unknown2 == ["extra"]

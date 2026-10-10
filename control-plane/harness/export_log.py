#!/usr/bin/env python3
"""Convert experiments/log.jsonl into the official `experimentos.jsonl` format.

Official format from the challenge statement: one line per request to the original with
  {id, seq, x_req, hipotesis, request, status, response, conclusion}
where:
  - id        = log line number (the `n` field).
  - seq       = the log section (the `section` field).
  - x_req     = the X-Req header of the response (if any).
  - hipotesis = a readable sentence for the section, taken from the editable
                map harness/hipotesis.json (one sentence per section). If the
                section is missing, a proposal is added to the map.
  - request   = the body sent to /msg ({"m": ...}); for /__reset and raw
                requests it is described as {"metodo","ruta"} (+ body if any).
  - status    = HTTP status of the original.
  - response  = the original's body parsed as JSON (if it was) or string.
  - conclusion= optional (empty by default; editable).

Writes spec/experimentos.jsonl and (re)generates/updates harness/hipotesis.json.

NOTE: the output field names (hipotesis, request, metodo, ruta, ...) and every
hypothesis sentence below are kept in Spanish on purpose: they are the official
deliverable format and the content of hipotesis.json / experimentos.jsonl, which
must stay byte-compatible.

CLI:
  python3 export_log.py                 # generate spec/experimentos.jsonl
  python3 export_log.py --out PATH
  python3 export_log.py --print-map     # only print the hypothesis map
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "experiments", "log.jsonl")
HIP_MAP = os.path.join(HERE, "hipotesis.json")
REPO = os.path.dirname(ROOT)  # repository root (spec/ and rules/ are shared by both planes)
OUT = os.path.join(REPO, "spec", "experimentos.jsonl")

# Proposed sentence per known section (editable in harness/hipotesis.json).
PROPOSALS = {
    "replay-ejemplos-oficiales": "Reproducción de los 20 ejemplos oficiales tras reset",
    "estado-inicial-y-consulta": "Estado inicial vacío tras reset y formato exacto de CONSULTA",
    "alta-edge-y-formato-id": "ALTA: casos límite del nombre y formato del id de cuenta",
    "deposito-edge-y-orden": "DEPOSITO: validación del monto y orden frente a la cuenta",
    "deposito-max-monto": "DEPOSITO: monto máximo admitido (9999999999) y tope",
    "transfer-limite-boundary": "TRANSFER: límite por operación en el borde 100000/100001",
    "cuenta-bloqueada-caracterizar": "Alcance del bloqueo: qué sigue permitido con la cuenta bloqueada",
    "bloqueo-umbral-exacto": "Umbral exacto de bloqueo (3 transferencias fallidas)",
    "transfer-orden-validacion": "TRANSFER: orden de evaluación de errores",
    "anula-semantica-y-numeracion": "ANULA: semántica de reversión y numeración de OP",
    "anula-saldo-negativo": "ANULA revierte incondicionalmente y puede dejar saldo negativo",
    "bloqueo-orden-y-e507": "Bloqueo: E507 cuenta como fallo y orden frente a E510",
    "comandos-ocultos": "Comandos inexistentes responden E301 (comando desconocido)",
    "auth-y-ruteo": "Autenticación Bearer y ruteo: orden ruta->auth->handler",
    "cuerpo-grande-E101": "Umbral de cuerpo demasiado grande (E101, > 8192 bytes)",
    "pista-readme-PING-sin-control": "PING no valida el valor del control (pista del README)",
    "orden-aridad-vs-control": "Orden de validación: control antes que aridad y comando",
    "battery": "Batería de regresión de 27 pasos que ejercita todas las reglas conocidas",
    "comandos-ocultos-HALT-NOTAS": "Comandos ocultos HALT (aridad 1 -> 409 E520 sin alterar estado) y NOTAS (aridad 0 -> 200 lista fija); E301 con 'sugerencia' por Levenshtein <= 2",
    "monitor": "Monitor de deriva: ronda de la batería de 27 pasos contra el original (debe ser idéntica a la línea base)",
    "monitor-baseline-cmds": "Monitor: línea base de 62 nombres de comando candidatos (todos E301) para detectar comandos nuevos y cambios en 'sugerencia'",
    "monitor-sondeo-aridad": "Monitor: sondeo de aridad de cada comando conocido (debe seguir dando E302)",
    "monitor-ruido": "Monitor: piso de ruido, dos rondas seguidas de la batería de 27 pasos deben ser idénticas entre sí y a la línea base",
    "monitor-contrato": "Monitor: contrato HTTP (/health con y sin credencial, /__reset, ruta inexistente) debe seguir igual",
    "spec-segunda-observacion": "Segunda observación de cada par cerrado en spec-cierre-pares-probables (token ajeno, sin esquema, E202/E201 antes que E301/E302, E303 antes que E304, E304 antes que E401, control 050, m null, cuerpo vacío, control de HALT)",
    "monitor-sondeo-cmds": "Monitor: sondeo de nombres de comando candidatos (todos deben seguir E301 con la misma 'sugerencia')",
    "difffuzz-smoke": "Fuzz diferencial (humo): mismas respuestas original vs réplica",
    "difffuzz-single": "Fuzz diferencial: requests sueltos (nombres de comando con errores tipográficos, espacios, controles) para comparar original vs réplica",
    "difffuzz-alta": "Fuzz diferencial: variantes de ALTA (nombres)",
    "difffuzz-alta2": "Fuzz diferencial: variantes de ALTA (segunda tanda)",
    "difffuzz-alta3": "Fuzz diferencial: variantes de ALTA (tercera tanda)",
    "difffuzz-amount": "Fuzz diferencial: formatos de monto en DEPOSITO/TRANSFER",
    "difffuzz-logic": "Fuzz diferencial: secuencias con estado (bloqueo, ANULA, numeración)",
    "difffuzz-sval": "Fuzz diferencial: validación de segmentos y controles",
    "difffuzz-classify": "Fuzz diferencial: clasificación de diferencias encontradas",
    "spec-cierre-pares-probables": "Cierre de pares no probados de la spec: credencial registrada (auth) para ruta->auth y variantes del header; E202/E201 antes que E301/E302; E303 antes que E304; E304 antes que E401; control '03' rechazado; m null y cuerpo vacío; control de HALT",
    "pending: ALTA nombres no-ASCII, control descartando vs normalizando": "ALTA con nombres no-ASCII: el control descarta los caracteres no-ASCII (no los normaliza a ASCII)",
    "pending: controles atípicos 00 / +5 / 3 dígitos": "Formato del control: '00' -> E202 (valor), '+5', '014' y '123' -> E201 (sólo 1-2 dígitos)",
    "pending: reinicio del contador de bloqueo tras un éxito": "El contador de fallos del bloqueo se reinicia con una TRANSFER exitosa (bloqueo = 3 fallos consecutivos)",
    "pending: ¿límite acumulado? 3 transferencias de 60000 seguidas": "¿Límite acumulado de TRANSFER? 3 x 60000 seguidas: todas 200, sin tope acumulado",
    "pending: ¿tope de cuentas? crear 30 cuentas": "¿Tope de cuentas? 30 ALTA seguidas: todas 201 hasta AC-0030, sin tope",
}


def generic_proposal(s):
    """Sentence for harness sections (single:/seq-*/replay:) with no map entry."""
    if s.startswith("single:"):
        return s[len("single:"):]
    if s.startswith("seq-short:gen "):
        return f"Secuencia corta generada ({s.split()[-1]} pasos) para grabar respuestas de referencia del original"
    if s.startswith("seq-long:gen "):
        return f"Secuencia larga generada ({s.split()[-1]} pasos) para grabar respuestas de referencia del original"
    if s.startswith("seq-short:"):
        return "Secuencia corta: " + s[len("seq-short:"):]
    if s.startswith("seq-long:"):
        return "Secuencia larga: " + s[len("seq-long:"):]
    if s.startswith("replay:"):
        return (f"Re-ejecución contra el original de '{s[len('replay:'):]}' para "
                "reconfirmar que las respuestas se repiten (verificación de la réplica)")
    return s or "(sin sección)"


def load_map():
    if os.path.exists(HIP_MAP):
        try:
            return json.load(open(HIP_MAP))
        except (ValueError, OSError):
            pass
    return {}


def ensure_map(sections):
    m = load_map()
    changed = False
    for s in sections:
        if s not in m:
            m[s] = PROPOSALS.get(s, generic_proposal(s))
            changed = True
    if changed or not os.path.exists(HIP_MAP):
        with open(HIP_MAP, "w") as f:
            json.dump(m, f, ensure_ascii=False, indent=2, sort_keys=True)
    return m


def request_of(req):
    """Derive the official `request` field from the log's req."""
    body = req.get("body")
    path = req.get("path")
    method = req.get("method")
    if path == "/msg" and isinstance(body, dict):
        out = dict(body)
    else:
        out = {"metodo": method, "ruta": path}
        if body is not None:
            out["body"] = body
    # credential used (none / lowercase-bearer / foreign-token / no-scheme /
    # valid); only lines >= L1059 record it.
    if req.get("auth_kind"):
        out["auth"] = req["auth_kind"]
    return out


def response_of(res):
    body = res.get("body")
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return body


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--print-map", action="store_true")
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(LOG)]
    sections = []
    for r in rows:
        s = r.get("section", "")
        if s not in sections:
            sections.append(s)
    hmap = ensure_map(sections)

    if args.print_map:
        print(json.dumps(hmap, ensure_ascii=False, indent=2, sort_keys=True))
        return

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    n = 0
    with open(args.out, "w") as f:
        for r in rows:
            sec = r.get("section", "")
            rec = {
                "id": r["n"],
                "seq": sec or "sin-seccion",
                "x_req": r["res"].get("x_req"),
                "hipotesis": hmap.get(sec, sec),
                "request": request_of(r["req"]),
                "status": r["res"]["status"],
                "response": response_of(r["res"]),
                "conclusion": "",
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"[export] {n} lines -> {os.path.relpath(args.out, ROOT)}")
    print(f"[export] hypothesis map ({len(hmap)} sections) -> "
          f"{os.path.relpath(HIP_MAP, ROOT)}")


if __name__ == "__main__":
    main()

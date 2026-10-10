#!/usr/bin/env python3
"""Convert experiments/log.jsonl into the official spec/experimentos.jsonl format.

One line per request to the original:
  {id, seq, x_req, hipotesis, request, status, response, conclusion}
  - id        = position in experiments/log.jsonl (1-based). The log's own `n`
                collided 6 times under concurrent appends, so it is not used;
                genspec.py cites the same positions, so every `Lnn` is an `id`.
  - seq       = the log `section`.
  - x_req     = response X-Req header (number when numeric, else raw, null when absent).
  - hipotesis = one sentence per section from harness/hipotesis.json (editable;
                missing or boilerplate entries are replaced by a proposal).
  - request   = the body sent, as the README shows it ({"m": ...} for /msg); other
                requests are described as {"metodo","ruta"} (+ "body"). The
                credential used, when not the valid token, goes to `conclusion`.
  - status    = HTTP status (0 = network error).
  - response  = body parsed as JSON when possible, else string.
  - conclusion= short sentence derived from status/response.

Field names and hypothesis sentences are the deliverable's content (frozen).

CLI:
  python3 export_log.py                 # generate spec/experimentos.jsonl
  python3 export_log.py --out PATH
  python3 export_log.py --print-map     # only print the hypothesis map
"""
import argparse
import json
import os
import re

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
    # --- sections found by differential fuzzing / hidden run (14:55-15:20)
    "hidden-run2": "Replay de lo que el evaluador envió en la ronda 2 (secuencias ocultas) contra el original para obtener la respuesta esperada de cada paso y corregir la réplica donde difería",
    "probe-unblock": "Desbloqueo por depósito: ¿un DEPOSITO a la cuenta bloqueada la desbloquea y reinicia el contador? ¿y un depósito a otra cuenta, CONSULTA, transferencia entrante, DEPOSITO inválido o ANULA?",
    "probe-fold": "Plegado de mayúsculas del nombre del comando: ¿es sólo ASCII? 'pıng' (i sin punto), 'PİNG', 'ſalta' y 'Kalta' (signo Kelvin) frente a 'ping'/'Ping'",
    "probe-bigbody": "Cuerpo JSON grande con claves extra: ¿el límite de 8192 bytes es inclusivo y se procesa el m aunque haya otras claves?",
    # --- 15:30 incident sweep (first requests against version 2 of the original)
    "incidente-acumulado": "Incidente: ¿cambió el límite por operación o el bloqueo? Tres TRANSFER de 100000 seguidas y la cuarta, luego CONSULTA del saldo",
    "incidente-muchas-cuentas": "Incidente: ¿cambió el formato o la numeración de los ids de cuenta al crear muchas cuentas?",
    "incidente-muchas-ops": "Incidente: ¿cambió la numeración de OP o el formato del saldo tras muchos depósitos pequeños?",
    "incidente-montos": "Incidente: ¿cambiaron las reglas de monto (máximo 9999999999, negativos, decimales, ceros a la izquierda) y el límite por operación?",
    "incidente-nombres": "Incidente: ¿cambió la validación del nombre en ALTA (un carácter, dígitos, espacios, 80 caracteres, guion, minúsculas, vacío)?",
    "incidente-saldo-exacto": "Incidente: formato exacto del saldo en CONSULTA tras depósitos y transferencias (¿entero o cadena con decimales?)",
    "incidente-anula": "Incidente: ¿cambió la semántica de ANULA (reversión, 'ya anulada', OP inexistente u OP-0000) y el saldo resultante?",
    "incidente-strikes": "Incidente: ¿cambiaron el umbral de bloqueo, qué fallos cuentan como strike (E401/E304/E303 no) y el reinicio tras una TRANSFER exitosa?",
    "incidente-halt": "Incidente: ¿siguen igual NOTAS, HALT sin confirmar (409 E520) y HALT;1 (503 E599 hasta el reset)?",
    "incidente-bisect": "Incidente: bisección sobre estado limpio del nuevo límite por operación (99999 … 50001 -> E506, 50000 -> 200), formato del saldo ('0.50', '-1.00') y contador 'intentos' del 423 (hasta 12; se reinicia al desbloquear por depósito)",
    # --- fuzz probes recorded with a truncated English label
    "fuzz-b1-probe:same-name accounts: is the per-operation limit e": "Cuentas del mismo titular (mismo nombre en ALTA): ¿están exentas del límite por operación? TRANSFER de 100001 entre dos 'Lucia'",
    "fuzz-b1-probe:control: name X1 vs different name Mateo, over l": "Control: titulares distintos (X1 vs Mateo) con el mismo monto 100001 -> ¿E506? (la exención es sólo para el mismo nombre)",
    "fuzz-b1-probe:same-name, balance 0, over limit x3: E507 and do": "Mismo titular con saldo 0 y monto sobre el límite x3: ¿E507 (la exención no evita saldo insuficiente) y bloqueo E510 al tercer fallo?",
    "fuzz-b1-probe:same-name case-insensitive? Lucia vs lucia, over": "¿La exención de mismo titular distingue mayúsculas? 'Lucia' vs 'lucia' con 100001",
}

_FUZZ_RE = re.compile(r"^fuzz-b(\d+):fuzz (long|short) (\d+) steps \(seed (\d+)\)$")
_FUZZ_SINGLE_RE = re.compile(r"^fuzz-b(\d+):fuzz single: (.*)$")
_FUZZ_PROBE_RE = re.compile(r"^fuzz-b(\d+)-probe:(.*)$")


def generic_proposal(s):
    """Sentence for harness sections (single:/seq-*/replay:/fuzz-b*/rerecord:) with no map entry."""
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
    if s.startswith("rerecord:"):
        return (f"Regrabación contra el original de la secuencia '{s[len('rerecord:'):]}' "
                "(su primera grabación tuvo un error de red) para fijar la respuesta esperada")
    m = _FUZZ_RE.match(s)
    if m:
        batch, kind, steps, seed = m.groups()
        largo = "larga" if kind == "long" else "corta"
        return (f"Secuencia aleatoria {largo} de {steps} pasos generada por el modelo "
                f"del libro mayor (lote {batch}, semilla {seed}) para grabar la respuesta "
                "del original y compararla con la réplica")
    m = _FUZZ_SINGLE_RE.match(s)
    if m:
        batch, what = m.groups()
        return (f"Request suelto de fuzzing (lote {batch}): {what}; se graba la respuesta "
                "del original para compararla con la réplica")
    m = _FUZZ_PROBE_RE.match(s)
    if m:
        batch, what = m.groups()
        return f"Sonda dirigida tras el fuzzing (lote {batch}): {what}"
    return s or "(sin sección)"


def proposal_for(section):
    return PROPOSALS.get(section, generic_proposal(section))


def load_map():
    if os.path.exists(HIP_MAP):
        try:
            return json.load(open(HIP_MAP))
        except (ValueError, OSError):
            pass
    return {}


def ensure_map(sections):
    """Load the map, add missing sections, replace boilerplate entries (sentence == section name); keep hand-edited ones."""
    m = load_map()
    changed = False
    for s in sections:
        prop = proposal_for(s)
        if s not in m or (m[s] == s and prop != s):
            m[s] = prop
            changed = True
    if changed or not os.path.exists(HIP_MAP):
        with open(HIP_MAP, "w") as f:
            json.dump(m, f, ensure_ascii=False, indent=2, sort_keys=True)
    return m


def request_of(req):
    """Official `request` field: the body sent ({"m": ...}); a raw body that is valid JSON is
    parsed, anything else is described with metodo/ruta (+ body)."""
    body = req.get("body")
    path = req.get("path")
    method = req.get("method")
    if path == "/msg" and isinstance(body, dict):
        return dict(body)
    if path == "/msg" and isinstance(body, str):
        try:
            return json.loads(body)
        except ValueError:
            pass
    out = {"metodo": method, "ruta": path}
    if body is not None:
        out["body"] = body
    return out


def response_of(res):
    body = res.get("body")
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return body


def x_req_of(res):
    x = res.get("x_req")
    if isinstance(x, str) and x.isdigit():
        return int(x)
    return x


# Credential used, as recorded in the log (only lines >= L1059 record it).
AUTH_TEXT = {
    "none": "sin header Authorization",
    "lowercase-bearer": "esquema 'bearer' en minúscula",
    "foreign-token": "token de otro equipo",
    "no-scheme": "token sin esquema Bearer",
}


def conclusion_of(req, res):
    """Short sentence derived from status/response (what the original did)."""
    status = res.get("status")
    body = response_of(res)
    if status == 0:
        c = "sin respuesta (error de red / timeout), no es una respuesta del servicio"
    elif isinstance(body, dict):
        if "error" in body:
            c = f"{status} {body['error']} {body.get('mensaje', '')}".rstrip()
            if "sugerencia" in body:
                c += f" (sugerencia {body['sugerencia']})"
            if "intentos" in body:
                c += f" (intentos {body['intentos']})"
        elif "pong" in body:
            c = f"{status} pong"
        elif "cuentaId" in body:
            c = f"{status} cuenta creada {body['cuentaId']}"
        elif "operacionId" in body:
            c = f"{status} {body.get('status', 'ok')} {body['operacionId']}"
        elif "saldo" in body:
            c = f"{status} saldo de {body.get('cuenta_id', '?')} = {json.dumps(body['saldo'])}"
        elif "anulada" in body:
            c = f"{status} anulada {body['anulada']}"
        elif "motivo" in body:
            c = f"{status} ok=false, motivo '{body['motivo']}'"
        elif "notas" in body:
            c = f"{status} lista de {len(body['notas'])} nota(s) con texto fijo"
        elif body.get("ok") is True:
            c = f"{status} ok"
        else:
            c = f"{status} {json.dumps(body, ensure_ascii=False)[:80]}"
    else:
        c = f"{status} {str(body)[:80]}" if body else f"{status} sin cuerpo"
    kind = req.get("auth_kind")
    if kind and kind != "valid":
        c += f" (credencial: {AUTH_TEXT.get(kind, kind)})"
    return c


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
        for pos, r in enumerate(rows, 1):
            sec = r.get("section", "")
            rec = {
                "id": pos,  # line number of the export (== position in the log); unique
                "seq": sec or "sin-seccion",
                "x_req": x_req_of(r["res"]),
                "hipotesis": hmap.get(sec) or proposal_for(sec),
                "request": request_of(r["req"]),
                "status": r["res"]["status"],
                "response": response_of(r["res"]),
                "conclusion": conclusion_of(r["req"], r["res"]),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"[export] {n} lines -> {os.path.relpath(args.out, ROOT)}")
    print(f"[export] hypothesis map ({len(hmap)} sections) -> "
          f"{os.path.relpath(HIP_MAP, ROOT)}")


if __name__ == "__main__":
    main()

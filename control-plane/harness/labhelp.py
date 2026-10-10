#!/usr/bin/env python3
"""Shared laboratory utilities: every request to the ORIGINAL goes through experiments/cn.py
(logged in experiments/log.jsonl) and is throttled below 8 req/s.

- og_reset(section) / og_msg(m, section) / og_raw(method, path, body, section, token=cn.TOK)
  -> (status, body_str); token=None sends no Authorization (contract check).
- log_count(): log lines so far (requests_spent = final - initial).
- BATTERY / battery_messages(): the monitor's 27-step battery, each step tagged with its rule.
- PROBE_COMMANDS: candidate names for the monitor's probing.

Library only (imported by record/replay/monitor).
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "experiments"))

import cn        # noqa: E402  client+logger (reads .env, appends to the log)
import control   # noqa: E402  control ISO 7064 MOD 97-10

LOG = cn.LOG
ORIGINAL_URL = cn.URL
MIN_INTERVAL = 0.14  # ~7.1 req/s < 8 req/s

_last = [0.0]


def throttle():
    """Block just enough not to exceed ~7 req/s towards the original."""
    dt = time.time() - _last[0]
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last[0] = time.time()


def og_reset(section=""):
    throttle()
    return cn.reset(section=section)


def og_msg(m, section=""):
    throttle()
    return cn.msg(m, section=section)


def og_raw(method, path, raw_body=None, section="", token=cn.TOK):
    """Raw request against the original; token=None sends it without Authorization (contract check)."""
    throttle()
    return cn.call(method, path, raw_body=raw_body, section=section, token=token)


def log_count():
    try:
        with open(LOG) as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


# --- Monitor battery (27 steps after reset): (kind, payload, rule). kind="full" = literal message,
# kind="ctrl" = control computed with control.message(). `rule` is written verbatim to corpus/battery.jsonl.
BATTERY = [
    ("full", "PING;64", "R-PING-ok: PING responde pong con control válido"),
    ("full", "PING;XX", "R-PING-ignora-control: PING no valida el valor del control"),
    ("ctrl", "SALDOS;AC-0001", "R-E301: comando desconocido (mensaje con espacio final)"),
    ("ctrl", "ALTA;Lucia;Gomez", "R-E302: aridad inválida (ALTA espera 1 arg)"),
    ("ctrl", "ALTA;Lucia", "R-ALTA: alta crea cuenta secuencial AC-0001"),
    ("ctrl", "ALTA;Mateo", "R-ALTA: AC-0002"),
    ("ctrl", "ALTA;Ana", "R-ALTA: AC-0003"),
    ("ctrl", "CONSULTA;AC-0001", "R-CONSULTA: saldo inicial 0, snake_case"),
    ("ctrl", "CONSULTA;AC-0009", "R-E401: cuenta inexistente (404)"),
    ("ctrl", "DEPOSITO;AC-0001;250000", "R-DEPOSITO: acredita, OP secuencial, typo recieved"),
    ("ctrl", "DEPOSITO;AC-0001;0", "R-E303: monto inválido (0)"),
    ("ctrl", "DEPOSITO;AC-0009;100", "R-DEPOSITO-E401: cuenta inexistente con monto válido"),
    ("ctrl", "TRANSFER;AC-0001;AC-0001;10", "R-E304: origen y destino iguales"),
    ("ctrl", "TRANSFER;AC-0001;AC-0002;100000", "R-TRANSFER: límite 100000 inclusive OK"),
    ("ctrl", "TRANSFER;AC-0001;AC-0002;100001", "R-E506: límite excedido (>100000) strike 1"),
    ("ctrl", "TRANSFER;AC-0001;AC-0099;200000", "R-TRANSFER-E401: destino inexistente antes que límite"),
    ("ctrl", "TRANSFER;AC-0003;AC-0002;50000", "R-E507: saldo insuficiente (origen AC-0003 en 0)"),
    ("ctrl", "TRANSFER;AC-0001;AC-0002;100001", "R-E506: strike 2"),
    ("ctrl", "TRANSFER;AC-0001;AC-0002;100001", "R-E506: strike 3 -> bloqueo"),
    ("ctrl", "TRANSFER;AC-0001;AC-0002;100", "R-E510: cuenta bloqueada tras 3 fallos"),
    ("ctrl", "CONSULTA;AC-0001", "R-MEMORIA: saldo refleja operaciones previas"),
    ("ctrl", "TRANSFER;AC-0002;AC-0003;60000", "R-TRANSFER-ok: desde otra cuenta"),
    ("ctrl", "ANULA;OP-0002", "R-ANULA: revierte la transferencia OP-0002"),
    ("ctrl", "CONSULTA;AC-0002", "R-SALDO-NEG: la anulación puede dejar saldo negativo"),
    ("ctrl", "ANULA;OP-0002", "R-ANULA-repetida: ya anulada"),
    ("ctrl", "ANULA;OP-9999", "R-E402: operación inexistente (404)"),
    ("ctrl", "DEPOSITO;AC-0003;700", "R-DEPOSITO-ok: cierre de la batería"),
]


def build_message(kind, payload):
    return payload if kind == "full" else control.message(payload)


def battery_messages():
    """Return [(message, rule), ...] ready to send after a reset."""
    return [(build_message(k, p), r) for (k, p, r) in BATTERY]


# Candidate commands for the monitor's long-round probing.
PROBE_COMMANDS = [
    "RETIRO", "SALDO", "EXTRACTO", "MOVIMIENTOS", "BLOQUEO", "DESBLOQUEO",
    "BAJA", "CIERRE", "TRANSFERENCIA", "PAGO", "COMISION", "LIMITE",
    "HISTORIAL", "ESTADO", "VERSION", "HELP",
]

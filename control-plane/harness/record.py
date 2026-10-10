#!/usr/bin/env python3
"""Record sequences against the ORIGINAL service and store them as cases.

Each case is `{case_id, hipotesis, steps:[{m|raw, status, body}]}` recorded
after a `POST /__reset`. Every request to the original goes through
`experiments/cn.py` (labhelp.og_*), so it is appended to
`experiments/log.jsonl` with a descriptive section that doubles as a
hypothesis. Traffic is kept below 8 req/s.

Groups (written to corpus/*.jsonl):
  (a) battery  -> corpus/battery.jsonl  : the monitor's 27-step battery.
  (b) singles  -> corpus/singles.jsonl  : ~42 single-request sequences
      (each command and each error reachable with a single message).
  (c) sequences-> corpus/sequences-short.jsonl (~45 of 2-5 steps) and
      corpus/sequences-long.jsonl (~15 of 6-12 steps), generated from a simple
      ledger model.
  (d) pending  -> corpus/pending.jsonl  : the analysts' pending experiments
      (accents/ñ/emoji in ALTA, block-counter reset, cumulative limit, account
      cap, "00"/"+5"/3-digit controls).

The corpus payloads, hypothesis sentences, section labels and "rule"/"variante"
fields are kept in Spanish on purpose: they are written verbatim into the
byte-frozen corpus/*.jsonl files and the hypothesis map.

CLI:
  python3 record.py all          # (a)+(b)+(c)+(d)  [default]
  python3 record.py battery
  python3 record.py singles
  python3 record.py sequences
  python3 record.py pending
  python3 record.py --budget 1200   # request cap against the original for this run

PARTIAL re-recording (#7; additive, re-records everything by default):
  python3 record.py --only-cases battery-27,single-05   # only those, merges
  python3 record.py --from-diff lab/evidence/5_replay_v2.txt  # those hit by the diff
  (both keep the rest of the corpus cases and can be combined with --target/--out-dir)
"""
import argparse
import json
import os
import random
import sys
import unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import labhelp as L          # noqa: E402
import control               # noqa: E402

ROOT = L.ROOT
CORPUS = os.path.join(ROOT, "corpus")
os.makedirs(CORPUS, exist_ok=True)


# --- Transport (additive). Without --target: ORIGINAL via cn.py (logged +
# throttled), behaviour identical to before. With --target/--token: a LOCAL
# emulator over httpx, in-memory budget (does not touch experiments/log.jsonl
# nor the real corpus unless --out-dir is given).
class OrigTransport:
    def reset(self, section=""):
        return L.og_reset(section=section)

    def msg(self, m, section=""):
        return L.og_msg(m, section=section)

    def raw(self, method, path, body=None, section=""):
        return L.og_raw(method, path, raw_body=body, section=section)

    def count(self):
        return L.log_count()


class TargetTransport:
    def __init__(self, target, token):
        import httpx
        self.base = target.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=self.base, headers=headers, timeout=15.0)
        self.n = 0

    def _do(self, method, path, body=None, raw=None):
        self.n += 1
        h = {}
        data = None
        if raw is not None:
            data = raw.encode()
            h["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        resp = self.client.request(method, path, content=data, headers=h)
        return resp.status_code, resp.text

    def reset(self, section=""):
        return self._do("POST", "/__reset")

    def msg(self, m, section=""):
        return self._do("POST", "/msg", body={"m": m})

    def raw(self, method, path, body=None, section=""):
        return self._do(method, path, raw=body)

    def count(self):
        return self.n


OG = OrigTransport()

# PARTIAL re-recording (#7). SELECT=None => record everything (the usual
# behaviour); SELECT={ids} => record ONLY those case_id and merge them into the
# existing corpus, keeping the rest of the cases as-is.
SELECT = None


def selected(case_id):
    return SELECT is None or case_id in SELECT


def parse_diff_report(path):
    """Extract the case_id values from a replay.py diff report. replay prints
    each diff as '   <case_id> paso <i> [<cmd>]: <reason>'. The 'paso' token is
    part of that fixed, cross-script format and must not change."""
    import re
    ids = set()
    pat = re.compile(r"^\s+(\S+)\s+paso\s+\d+\s+\[")
    with open(path) as f:
        for line in f:
            mt = pat.match(line)
            if mt:
                ids.add(mt.group(1))
    return ids


# case_id prefixes per group, to skip a whole group when no selected id falls
# into it (partial re-recording).
GROUP_PREFIXES = {
    "battery": ("battery-",),
    "singles": ("single-",),
    "sequences": ("seq-short-", "seq-long-"),
    "pending": ("pending-",),
}


def group_has_selection(group):
    if SELECT is None:
        return True
    return any(cid.startswith(p) for cid in SELECT for p in GROUP_PREFIXES[group])


# Pool of varied amounts, includes the cap and the first invalid value.
AMOUNTS = [1, 100, 700, 50000, 100000, 100001, 250000,
           1000000000, 9999999999, 10000000000, 0]


class Budget:
    def __init__(self, cap):
        self.cap = cap
        self.start = OG.count()

    def spent(self):
        return OG.count() - self.start

    def check(self):
        if self.spent() >= self.cap:
            raise SystemExit(f"[record] budget exhausted: {self.spent()} >= {self.cap}")


def run_case(budget, case_id, hipotesis, steps, section):
    """Reset + send each step. steps: list of str (/msg message) or
    dict {'raw': {method, path, body}}. Returns the case with what was observed,
    or None if the case is not selected (partial re-recording)."""
    if not selected(case_id):
        return None
    budget.check()
    OG.reset(section=section)
    out_steps = []
    for st in steps:
        budget.check()
        if isinstance(st, dict) and "raw" in st:
            r = st["raw"]
            status, body = OG.raw(r["method"], r["path"], body=r.get("body"), section=section)
            out_steps.append({"raw": r, "status": status, "body": body})
        else:
            status, body = OG.msg(st, section=section)
            out_steps.append({"m": st, "status": status, "body": body})
    return {"case_id": case_id, "hipotesis": hipotesis, "steps": out_steps}


def write_cases(path, cases):
    cases = [c for c in cases if c is not None]
    if SELECT is not None and os.path.exists(path):
        # merge: keep existing cases and replace/add the re-recorded ones
        new_by_id = {c["case_id"]: c for c in cases}
        merged, used = [], set()
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                c = json.loads(line)
                cid = c.get("case_id")
                if cid in new_by_id:
                    merged.append(new_by_id[cid])
                    used.add(cid)
                else:
                    merged.append(c)
        for c in cases:
            if c["case_id"] not in used:
                merged.append(c)
        out = merged
        note = f" (merge: {len(cases)} re-recorded over {len(out)} total)"
    else:
        out = cases
        note = ""
    with open(path, "w") as f:
        for c in out:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"[record] {len(out)} cases -> {os.path.relpath(path, ROOT)}{note}")


# --------------------------------------------------------------------------- (a)
def do_battery(budget):
    if not selected("battery-27"):
        return []
    msgs = L.battery_messages()
    budget.check()
    OG.reset(section="battery")
    steps = []
    for m, rule in msgs:
        budget.check()
        status, body = OG.msg(m, section="battery")
        steps.append({"m": m, "rule": rule, "status": status, "body": body})
    case = {"case_id": "battery-27",
            "hipotesis": "Batería de 27 pasos que ejercita todas las reglas conocidas",
            "steps": steps}
    write_cases(os.path.join(CORPUS, "battery.jsonl"), [case])
    return [case]


# --------------------------------------------------------------------------- (b)
def do_singles(budget):
    m = control.message
    # (hipotesis, step)  step = payload without control (computed) or raw/literal dict
    specs = [
        ("PING responde pong con control válido", m("PING")),
        ("PING sin control -> E103 falta control", "PING"),
        ("PING; (segmento de control vacío) -> E104/E103", "PING;"),
        ("PING con aridad 1 -> E302", "PING;64;64"),
        ("PING;XX: PING no valida el valor del control -> pong", "PING;XX"),
        ("ping en minúscula: comando case-insensitive -> pong", "ping;64"),
        ("ALTA crea cuenta AC-0001", m("ALTA;Lucia")),
        ("ALTA con 2 args -> E302 aridad inválida", m("ALTA;Lucia;Gomez")),
        ("ALTA con nombre vacío -> E104 segmento vacío", m("ALTA;")),
        ("ALTA con nombre solo dígitos -> 201", m("ALTA;12345")),
        ("CONSULTA de cuenta inexistente -> E401", m("CONSULTA;AC-0001")),
        ("CONSULTA id minúsculas (case-sensitive) -> E401", m("CONSULTA;ac-0001")),
        ("CONSULTA id sin zero-pad -> E401", m("CONSULTA;AC-1")),
        ("CONSULTA aridad 0 -> E302", m("CONSULTA")),
        ("DEPOSITO a cuenta inexistente (monto válido) -> E401", m("DEPOSITO;AC-0001;100")),
        ("DEPOSITO monto 0 -> E303 (monto antes que cuenta)", m("DEPOSITO;AC-0001;0")),
        ("DEPOSITO monto negativo -> E303", m("DEPOSITO;AC-0001;-5")),
        ("DEPOSITO monto decimal -> E303", m("DEPOSITO;AC-0001;1.5")),
        ("DEPOSITO monto no numérico -> E303", m("DEPOSITO;AC-0001;abc")),
        ("DEPOSITO con ceros a la izquierda -> E303", m("DEPOSITO;AC-0001;0100")),
        ("DEPOSITO >= 1e10 -> E303", m("DEPOSITO;AC-0001;10000000000")),
        ("DEPOSITO aridad 1 -> E302", m("DEPOSITO;AC-0001")),
        ("TRANSFER entre cuentas inexistentes -> E401", m("TRANSFER;AC-0001;AC-0002;100")),
        ("TRANSFER origen=destino -> E304", m("TRANSFER;AC-0001;AC-0001;100")),
        ("TRANSFER monto inválido 0 -> E303 (antes que E304/E401)", m("TRANSFER;AC-0001;AC-0002;0")),
        ("TRANSFER aridad 2 -> E302", m("TRANSFER;AC-0001;AC-0002")),
        ("ANULA operación inexistente -> E402", m("ANULA;OP-0001")),
        ("ANULA aridad 0 -> E302", m("ANULA")),
        ("Comando desconocido SALDOS -> E301 (espacio final)", m("SALDOS;AC-0001")),
        ("Comando desconocido RETIRO -> E301", m("RETIRO")),
        ("Control no numérico 'XX' -> E201 antes que comando desconocido", "NOEXISTE;XX"),
        ("Control numérico incorrecto -> E202", "ALTA;Lucia;83"),
        ("Control con una letra '5X' -> E201", "PING;5X"),
        ("Body JSON inválido -> E100", {"raw": {"method": "POST", "path": "/msg", "body": "{"}}),
        ("Body JSON sin m -> E102", {"raw": {"method": "POST", "path": "/msg", "body": "{}"}}),
        ("m vacío -> E103 falta control", ""),
        ("Body array -> E102 falta m", {"raw": {"method": "POST", "path": "/msg", "body": "[]"}}),
        ("m no string (123) -> E102", {"raw": {"method": "POST", "path": "/msg", "body": "{\"m\":123}"}}),
        ("Mensaje sin ';' (solo texto) -> E103 falta control", "HOLA"),
        ("GET /health con token -> 200 {\"ok\":true}", {"raw": {"method": "GET", "path": "/health"}}),
        ("Ruta desconocida -> E404", {"raw": {"method": "GET", "path": "/nope"}}),
        ("Método incorrecto en ruta conocida (POST /health) -> E404", {"raw": {"method": "POST", "path": "/health"}}),
        ("Comando en minúscula deposito -> E401 (case-insensitive)", m("deposito;AC-0001;100")),
    ]
    cases = []
    for i, (hip, step) in enumerate(specs, 1):
        cid = f"single-{i:02d}"
        cases.append(run_case(budget, cid, hip, [step], f"single:{hip[:48]}"))
    write_cases(os.path.join(CORPUS, "singles.jsonl"), cases)
    return cases


# --------------------------------------------------------------------------- (c)
def _acct(n):
    return f"AC-{n:04d}"


def gen_sequence(rng, nsteps):
    """Generate payloads (without control) from a simple ledger model.

    It does not predict results; it only produces plausible messages with
    carried state (created accounts, last OP, transfer attempts)."""
    payloads = []
    naccts = 0
    op = 0  # optimistic OP counter to reference from ANULA
    # start with 1-2 ALTA to have accounts
    for _ in range(rng.randint(1, 2)):
        naccts += 1
        payloads.append(f"ALTA;Nombre{naccts}")
    while len(payloads) < nsteps:
        choices = ["deposito", "transfer", "consulta", "alta", "anula", "overlimit"]
        pick = rng.choice(choices)
        if pick == "alta" or naccts == 0:
            naccts += 1
            payloads.append(f"ALTA;Nombre{naccts}")
        elif pick == "deposito":
            a = _acct(rng.randint(1, max(1, naccts)))
            amt = rng.choice(AMOUNTS)
            payloads.append(f"DEPOSITO;{a};{amt}")
            if amt not in (0, 10000000000):
                op += 1
        elif pick == "consulta":
            a = _acct(rng.randint(1, naccts + 1))  # sometimes a non-existent one
            payloads.append(f"CONSULTA;{a}")
        elif pick == "transfer":
            o = rng.randint(1, naccts)
            d = rng.randint(1, naccts + 1)
            if d == o:
                d = o % naccts + 1
            amt = rng.choice([100, 50000, 60000, 100000])
            payloads.append(f"TRANSFER;{_acct(o)};{_acct(d)};{amt}")
            op += 1
        elif pick == "overlimit":
            o = rng.randint(1, naccts)
            d = o % naccts + 1
            payloads.append(f"TRANSFER;{_acct(o)};{_acct(d)};100001")
        elif pick == "anula":
            ref = rng.randint(1, max(1, op))
            payloads.append(f"ANULA;OP-{ref:04d}")
    return payloads[:nsteps]


# Secuencias curadas que garantizan cobertura de reglas clave.
CURATED_SHORT = [
    ("depósito y consulta reflejan saldo",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;250000", "CONSULTA;AC-0001"]),
    ("transferencia dentro del límite mueve saldo",
     ["ALTA;Lucia", "ALTA;Mateo", "DEPOSITO;AC-0001;250000",
      "TRANSFER;AC-0001;AC-0002;100000", "CONSULTA;AC-0002"]),
    ("transferencia justo sobre el límite -> E506",
     ["ALTA;Lucia", "ALTA;Mateo", "DEPOSITO;AC-0001;250000",
      "TRANSFER;AC-0001;AC-0002;100001"]),
    ("saldo insuficiente -> E507",
     ["ALTA;Lucia", "ALTA;Mateo", "TRANSFER;AC-0001;AC-0002;100"]),
    ("depósito tope 9999999999 OK",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;9999999999", "CONSULTA;AC-0001"]),
    ("depósito 10000000000 -> E303",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;10000000000"]),
    ("anular depósito revierte saldo",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;250000", "ANULA;OP-0001", "CONSULTA;AC-0001"]),
    ("anular transferencia devuelve fondos al origen",
     ["ALTA;Lucia", "ALTA;Mateo", "DEPOSITO;AC-0001;250000",
      "TRANSFER;AC-0001;AC-0002;100000", "ANULA;OP-0002"]),
    ("anular dos veces -> ya anulada",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;250000", "ANULA;OP-0001", "ANULA;OP-0001"]),
    ("origen y destino iguales -> E304",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;250000", "TRANSFER;AC-0001;AC-0001;100"]),
    ("transferir a cuenta inexistente -> E401",
     ["ALTA;Lucia", "DEPOSITO;AC-0001;250000", "TRANSFER;AC-0001;AC-0099;100"]),
    ("numeración OP global entre cuentas",
     ["ALTA;Lucia", "ALTA;Mateo", "DEPOSITO;AC-0001;5000", "DEPOSITO;AC-0002;5000"]),
]

CURATED_LONG = [
    ("bloqueo tras 3 transferencias sobre el límite",
     ["ALTA;Lucia", "ALTA;Mateo", "DEPOSITO;AC-0001;9999999999",
      "TRANSFER;AC-0001;AC-0002;100001", "TRANSFER;AC-0001;AC-0002;100001",
      "TRANSFER;AC-0001;AC-0002;100001", "TRANSFER;AC-0001;AC-0002;100",
      "CONSULTA;AC-0001"]),
    ("cuenta bloqueada: DEPOSITO y recibir siguen OK",
     ["ALTA;Lucia", "ALTA;Mateo", "DEPOSITO;AC-0001;9999999999",
      "TRANSFER;AC-0001;AC-0002;100001", "TRANSFER;AC-0001;AC-0002;100001",
      "TRANSFER;AC-0001;AC-0002;100001", "DEPOSITO;AC-0001;500",
      "TRANSFER;AC-0002;AC-0001;100", "CONSULTA;AC-0001"]),
    ("anulación que deja saldo negativo en el destino",
     ["ALTA;Lucia", "ALTA;Mateo", "ALTA;Ana", "DEPOSITO;AC-0001;100000",
      "TRANSFER;AC-0001;AC-0002;100000", "TRANSFER;AC-0002;AC-0003;60000",
      "ANULA;OP-0002", "CONSULTA;AC-0002"]),
    ("varias altas y consultas intercaladas",
     ["ALTA;A", "ALTA;B", "ALTA;C", "CONSULTA;AC-0001", "DEPOSITO;AC-0002;777",
      "CONSULTA;AC-0002", "DEPOSITO;AC-0003;888", "CONSULTA;AC-0003"]),
    ("depósitos grandes y transferencias encadenadas",
     ["ALTA;A", "ALTA;B", "DEPOSITO;AC-0001;9999999999",
      "TRANSFER;AC-0001;AC-0002;100000", "TRANSFER;AC-0001;AC-0002;100000",
      "TRANSFER;AC-0001;AC-0002;100000", "CONSULTA;AC-0002", "CONSULTA;AC-0001"]),
]


def do_sequences(budget):
    rng = random.Random(20251010)
    short, long = [], []
    for i, (hip, pls) in enumerate(CURATED_SHORT, 1):
        cid = f"seq-short-cur-{i:02d}"
        steps = [control.message(p) for p in pls]
        short.append(run_case(budget, cid, hip, steps, f"seq-short:{hip[:48]}"))
    # fill up to ~45 short sequences with the generator
    i = len(CURATED_SHORT)
    while len(short) < 45:
        i += 1
        n = rng.randint(2, 5)
        pls = gen_sequence(rng, n)
        steps = [control.message(p) for p in pls]
        hip = f"secuencia generada de {n} pasos (modelo ledger)"
        short.append(run_case(budget, f"seq-short-gen-{i:02d}", hip, steps, f"seq-short:gen {n}"))
    for i, (hip, pls) in enumerate(CURATED_LONG, 1):
        cid = f"seq-long-cur-{i:02d}"
        steps = [control.message(p) for p in pls]
        long.append(run_case(budget, cid, hip, steps, f"seq-long:{hip[:48]}"))
    i = len(CURATED_LONG)
    while len(long) < 15:
        i += 1
        n = rng.randint(6, 12)
        pls = gen_sequence(rng, n)
        steps = [control.message(p) for p in pls]
        hip = f"secuencia generada de {n} pasos (modelo ledger)"
        long.append(run_case(budget, f"seq-long-gen-{i:02d}", hip, steps, f"seq-long:gen {n}"))
    write_cases(os.path.join(CORPUS, "sequences-short.jsonl"), short)
    write_cases(os.path.join(CORPUS, "sequences-long.jsonl"), long)
    return short + long


# --------------------------------------------------------------------------- (d)
def _strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def do_pending(budget):
    cases = []
    findings = []

    # (d1) ALTA with accented/non-ASCII names and emoji: control discarding non-ASCII (control.py)
    #      vs control normalizing accents to ASCII. Note which one is accepted.
    if selected("pending-nombres-no-ascii"):
        nombres = ["Jose", "José", "Muñoz", "Renée", "Ángela", "Ana😀"]
        sec = "pending: ALTA nombres no-ASCII, control descartando vs normalizando"
        budget.check()
        OG.reset(section=sec)
        steps = []
        for nm in nombres:
            payload = f"ALTA;{nm}"
            mA = control.message(payload)                 # discards non-ASCII
            cB = control.control(_strip_accents(payload))  # normalizes accents to ASCII
            mB = f"{payload};{cB}"
            budget.check()
            sA, bA = OG.msg(mA, section=sec)
            steps.append({"m": mA, "variante": "descarta-no-ascii", "status": sA, "body": bA})
            if mB != mA:
                budget.check()
                sB, bB = OG.msg(mB, section=sec)
                steps.append({"m": mB, "variante": "normaliza-acentos", "status": sB, "body": bB})
                findings.append((nm, "descarta", sA, bA, "normaliza", sB, bB))
            else:
                findings.append((nm, "descarta==normaliza", sA, bA, "-", "-", "-"))
        cases.append({"case_id": "pending-nombres-no-ascii",
                      "hipotesis": "ALTA con tildes/ñ/emoji: ¿el control descarta no-ASCII o normaliza acentos?",
                      "steps": steps})

    # (d2) is the block counter reset after a success?
    #      2 failures (E506) -> 1 success (100000) -> 2 failures; then try a valid transfer.
    sec = "pending: reinicio del contador de bloqueo tras un éxito"
    pls = ["ALTA;Origen", "ALTA;Destino", "DEPOSITO;AC-0001;9999999999",
           "TRANSFER;AC-0001;AC-0002;100001",   # failure 1 (E506)
           "TRANSFER;AC-0001;AC-0002;100001",   # failure 2 (E506)
           "TRANSFER;AC-0001;AC-0002;100000",   # success
           "TRANSFER;AC-0001;AC-0002;100001",   # failure 3 (cumulative) / failure 1 (consecutive)
           "TRANSFER;AC-0001;AC-0002;100001",   # failure 4 cumulative / failure 2 consecutive
           "TRANSFER;AC-0001;AC-0002;100"]      # probe: E510 => cumulative ; 200 => reset
    steps_msgs = [control.message(p) for p in pls]
    c2 = run_case(budget, "pending-reinicio-bloqueo",
                  "¿el contador de 3 fallos se reinicia tras una transferencia exitosa?",
                  steps_msgs, sec)
    if c2:
        cases.append(c2)
        sonda = c2["steps"][-1]
        findings.append(("reinicio-bloqueo", "sonda-final", sonda["status"], sonda["body"],
                         "paso7", c2["steps"][6]["status"], c2["steps"][6]["body"]))

    # (d3) cumulative limit? 3 x 60000 in a row (each <=100000 per operation)
    sec = "pending: ¿límite acumulado? 3 transferencias de 60000 seguidas"
    pls = ["ALTA;Origen", "ALTA;Destino", "DEPOSITO;AC-0001;9999999999",
           "TRANSFER;AC-0001;AC-0002;60000",
           "TRANSFER;AC-0001;AC-0002;60000",
           "TRANSFER;AC-0001;AC-0002;60000",
           "CONSULTA;AC-0002"]
    steps_msgs = [control.message(p) for p in pls]
    c3 = run_case(budget, "pending-limite-acumulado",
                  "¿existe un límite acumulado además del límite por operación?",
                  steps_msgs, sec)
    if c3:
        cases.append(c3)
        findings.append(("limite-acumulado", "t1", c3["steps"][3]["status"], c3["steps"][3]["body"],
                         "t3", c3["steps"][5]["status"], c3["steps"][5]["body"]))

    # (d4) account cap? create 30 ALTA
    if selected("pending-tope-cuentas"):
        sec = "pending: ¿tope de cuentas? crear 30 cuentas"
        budget.check()
        OG.reset(section=sec)
        steps = []
        last = None
        for k in range(1, 31):
            budget.check()
            s, b = OG.msg(control.message(f"ALTA;C{k}"), section=sec)
            steps.append({"m": control.message(f"ALTA;C{k}"), "status": s, "body": b})
            last = (s, b)
        cases.append({"case_id": "pending-tope-cuentas",
                      "hipotesis": "¿hay un tope en el número de cuentas? (se crean 30)",
                      "steps": steps})
        findings.append(("tope-cuentas", "cuenta-30", last[0], last[1], "-", "-", "-"))

    # (d5) controls "00", "+5", 3 digits over a valid message
    if not selected("pending-controles-atipicos"):
        write_cases(os.path.join(CORPUS, "pending.jsonl"), cases)
        print("\n[record] FINDINGS from pending experiments:")
        for f in findings:
            print("   ", f)
        return cases
    sec = "pending: controles atípicos 00 / +5 / 3 dígitos"
    budget.check()
    OG.reset(section=sec)
    OG.msg(control.message("ALTA;Lucia"), section=sec)  # creates AC-0001 for CONSULTA
    base = "CONSULTA;AC-0001"
    ctrl_ok = control.control(base)
    # NOTE: the variant labels are written as the "variante" field of
    # corpus/pending.jsonl, so they are kept in Spanish for byte-compatibility.
    variants = [
        ("00", f"{base};00"),
        ("+5", f"{base};+5"),
        ("3 dígitos", f"{base};{ctrl_ok:03d}" if ctrl_ok < 100 else f"{base};100"),
        ("3 dígitos literal 123", f"{base};123"),
    ]
    steps = [{"m": control.message("ALTA;Lucia"), "status": None, "body": "(setup AC-0001)"}]
    for label, msg in variants:
        budget.check()
        s, b = OG.msg(msg, section=sec)
        steps.append({"m": msg, "variante": label, "status": s, "body": b})
        findings.append((f"control {label}", msg, s, b, "-", "-", "-"))
    cases.append({"case_id": "pending-controles-atipicos",
                  "hipotesis": "¿cómo trata el servicio controles '00', '+5' y de 3 dígitos?",
                  "steps": steps})

    write_cases(os.path.join(CORPUS, "pending.jsonl"), cases)
    print("\n[record] FINDINGS from pending experiments:")
    for f in findings:
        print("   ", f)
    return cases


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("grupo", nargs="?", default="all",
                    choices=["all", "battery", "singles", "sequences", "pending"])
    ap.add_argument("--budget", type=int, default=1200,
                    help="request cap against the original for this run")
    ap.add_argument("--target", default=None,
                    help="URL of a LOCAL emulator to re-record against (without it, the original)")
    ap.add_argument("--token", default="lab", help="Bearer token for --target")
    ap.add_argument("--out-dir", default=None,
                    help="corpus output directory (default corpus/); "
                         "use it with --target so the real corpus is not overwritten")
    ap.add_argument("--only-cases", default="",
                    help="re-record ONLY these case_id (comma-separated) and merge them "
                         "into the existing corpus, keeping the rest (#7)")
    ap.add_argument("--from-diff", default=None,
                    help="read a replay.py diff report and re-record only the "
                         "affected case_id (unioned with --only-cases) (#7)")
    args = ap.parse_args()

    global OG, CORPUS, SELECT
    sel = set(s.strip() for s in args.only_cases.split(",") if s.strip())
    if args.from_diff:
        if not os.path.exists(args.from_diff):
            print(f"[record] --from-diff does not exist: {args.from_diff}", file=sys.stderr)
            sys.exit(2)
        fromdiff = parse_diff_report(args.from_diff)
        print(f"[record] --from-diff {args.from_diff}: {len(fromdiff)} affected case(s): "
              f"{sorted(fromdiff) or 'none'}")
        sel |= fromdiff
    if sel:
        SELECT = sel
        print(f"[record] PARTIAL re-recording of {len(SELECT)} case(s): {sorted(SELECT)}")

    if args.target:
        from urllib.parse import urlparse
        if urlparse(args.target).netloc.startswith("caja-negra"):
            print("[record] REJECTED: --target looks like the original; forbidden.", file=sys.stderr)
            sys.exit(3)
        OG = TargetTransport(args.target, args.token)
        print(f"[record] LOCAL target = {args.target} (token={args.token})")
    if args.out_dir:
        CORPUS = args.out_dir if os.path.isabs(args.out_dir) else os.path.join(ROOT, args.out_dir)
        os.makedirs(CORPUS, exist_ok=True)
        print(f"[record] out-dir = {CORPUS}")

    budget = Budget(args.budget)
    try:
        if args.grupo in ("all", "battery") and group_has_selection("battery"):
            do_battery(budget)
        if args.grupo in ("all", "singles") and group_has_selection("singles"):
            do_singles(budget)
        if args.grupo in ("all", "sequences") and group_has_selection("sequences"):
            do_sequences(budget)
        if args.grupo in ("all", "pending") and group_has_selection("pending"):
            do_pending(budget)
    finally:
        print(f"\n[record] requests spent in this run: {budget.spent()}")


if __name__ == "__main__":
    main()

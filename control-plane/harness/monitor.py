#!/usr/bin/env python3
"""Drift monitor for the ORIGINAL service (detects changes during the day).

Every N seconds it runs a CONTRACT CHECK (/health with and without token,
/__reset, 404 route) and the 27-step battery (labhelp.BATTERY) after a reset,
and compares them against the expected values (evaluator rule, compare.py). On a
diff it REPEATS the sequence with a reset and, only if it PERSISTS, prints an
ALERT with the message, the expected, the observed and the tagged rule. It
writes harness/drift.log, leaves everything in experiments/log.jsonl under the
"monitor" section (throttle < 8 req/s) and one line per round in
harness/metrics.jsonl.

Features (all additive; without --target the transport is still cn.py -> original):
  1. Per-round contract check: GET /health (with token -> 200 {"ok":true};
     without token -> 401 E001), POST /__reset (exact body {"ok":true}) and GET
     /__noexiste (404 E404). A diff here is an H/B-class ALERT with the same
     reset re-confirmation as the battery.
  2. Latency: ms per request (perf_counter) in the battery, contract and probes;
     per round it reports p50/p99/max, the number of responses > 5000 ms (the
     evaluator's threshold) and the number of 5xx, in drift.log and in
     harness/metrics.jsonl.
  3. Control-vs-control noise floor (--noise N): runs the battery N times at the
     start against the same target and counts how many steps differ between runs
     (should be 0). Noisy steps require 3 confirmations instead of 2.
  4. Infra vs service: a response with an X-Frontier-Infra header, status
     502/503/504 or a body containing "Application not found" is classified INFRA
     (not a change alert; counted separately and retried after --infra-wait s).
     Service signal = x-req.
  5. Name probing (long round): names from harness/sondas.json + near-misses at
     distance 1-2 from the real commands; it compares the "sugerencia" field of
     E301 against the baseline (a new suggestion = a new hidden command) and adds
     NOTAS;11 (arity 0) comparing the literal body. HALT is NEVER sent.
  6. Persistent daily budget (harness/budget.json, requests to the ORIGINAL per
     date): warning at 70% and automatic stop at 90% of 8000. Shown every round.
     (With a local --target no original budget is consumed or persisted.)

CLI:
  python3 monitor.py --once                 # a single round (leaves nothing running)
  python3 monitor.py --once --long          # force a long round (includes probing)
  python3 monitor.py --interval 180 --long-every 6 --rounds 0   # loop (0 = infinite)
  python3 monitor.py --build-baseline       # (re)record commands_baseline.jsonl today
  python3 monitor.py --noise 3 --once        # measure the noise floor (3 runs) and one round
  python3 monitor.py --target http://localhost:8191 --token lab --once
"""
import argparse
import json
import os
import sys
import time
from time import perf_counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import labhelp as L          # noqa: E402
import compare as C          # noqa: E402
import control               # noqa: E402
import cn                    # noqa: E402  (on sys.path via labhelp; for token=None)

ROOT = L.ROOT
CORPUS = os.path.join(ROOT, "corpus")
DRIFT_LOG = os.path.join(HERE, "drift.log")
METRICS_FILE = os.path.join(HERE, "metrics.jsonl")
BUDGET_FILE = os.path.join(HERE, "budget.json")
BATTERY_FILE = os.path.join(CORPUS, "battery.jsonl")
CMD_BASELINE = os.path.join(CORPUS, "commands_baseline.jsonl")
SONDAS_FILE = os.path.join(HERE, "sondas.json")

# Presupuesto diario contra el ORIGINAL.
DAILY_CAP = 8000
BUDGET_WARN = 0.70   # warning
BUDGET_STOP = 0.90   # automatic stop


# --- Infra vs service classification (#4) -----------------------------------
def is_infra(status, headers, body):
    """True if the response is from the INFRA (not the service): X-Frontier-Infra
    header, status 502/503/504, or a body with 'Application not found'."""
    hl = {k.lower(): v for k, v in (headers or {}).items()}
    if "x-frontier-infra" in hl:
        return True
    if status in (0, 502, 503, 504):  # 0 = network error recorded by cn.py
        return True
    if body and "Application not found" in body:
        return True
    return False


def has_service_signal(headers):
    """Signal that the SERVICE (not the infra) replied: x-req header."""
    return "x-req" in {k.lower() for k in (headers or {})}


# --- Per-round metrics (#2) ------------------------------------------------
class RoundMetrics:
    def __init__(self):
        self.times = []      # ms por request
        self.req = 0         # requests gastados en la ronda
        self.n_5xx = 0
        self.n_slow = 0      # > 5000 ms
        self.n_infra = 0
        self.no_xreq = 0     # respuestas del servicio sin x-req (sospechoso)
        self.steps = 0       # compared steps (contract + battery [+ probes])
        self.equal = 0       # pasos iguales a lo esperado/base
        self.alerts = 0

    def observe(self, ms, status, headers, body):
        """Hook called by the transport after EVERY request."""
        self.times.append(ms)
        self.req += 1
        if ms > 5000:
            self.n_slow += 1
        if isinstance(status, int) and status >= 500:
            self.n_5xx += 1
        if is_infra(status, headers, body):
            self.n_infra += 1
        elif headers and not has_service_signal(headers):
            self.no_xreq += 1

    def pcts(self):
        if not self.times:
            return 0.0, 0.0, 0.0
        s = sorted(self.times)
        p = lambda q: s[min(len(s) - 1, int(q * len(s)))]  # noqa: E731
        return round(p(0.50), 1), round(p(0.99), 1), round(s[-1], 1)


# --- Transports (additive plane). Without --target it talks to the ORIGINAL
# via cn.py (logged + throttled), behaviour identical to before. With
# --target/--token it talks over httpx to a LOCAL emulator. Both measure ms and
# (locally) expose headers, for latency and infra. They hook into an optional
# RoundMetrics (self.metrics) that observes every request.
class OrigTransport:
    def __init__(self):
        self.metrics = None
        self.last = {}

    def _timed(self, fn):
        t0 = perf_counter()
        st, body = fn()
        ms = (perf_counter() - t0) * 1000.0
        # cn.py does not return headers; only status/body are available here.
        self.last = {"ms": ms, "status": st, "headers": {}, "body": body}
        if self.metrics is not None:
            self.metrics.observe(ms, st, {}, body)
        return st, body

    def reset(self, section=""):
        return self._timed(lambda: L.og_reset(section=section))

    def msg(self, m, section=""):
        return self._timed(lambda: L.og_msg(m, section=section))

    def raw(self, method, path, body=None, section="", noauth=False):
        tok = None if noauth else cn.TOK
        return self._timed(lambda: L.og_raw(method, path, raw_body=body,
                                            section=section, token=tok))

    def count(self):
        return L.log_count()

    def is_local(self):
        return False


class TargetTransport:
    def __init__(self, target, token):
        import httpx
        self.base = target.rstrip("/")
        self.token = token
        self.client = httpx.Client(base_url=self.base, timeout=15.0)
        self.n = 0
        self.metrics = None
        self.last = {}

    def _do(self, method, path, body=None, raw=None, noauth=False):
        self.n += 1
        h = {}
        if not noauth and self.token:
            h["Authorization"] = f"Bearer {self.token}"
        data = None
        if raw is not None:
            data = raw.encode() if isinstance(raw, str) else raw
            h["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        t0 = perf_counter()
        try:
            resp = self.client.request(method, path, content=data, headers=h)
            ms = (perf_counter() - t0) * 1000.0
            st, text, headers = resp.status_code, resp.text, dict(resp.headers)
        except Exception as e:  # noqa: BLE001  (un request malformado no tumba la ronda)
            ms = (perf_counter() - t0) * 1000.0
            st, text, headers = None, f"<client-error: {type(e).__name__}>", {}
        self.last = {"ms": ms, "status": st, "headers": headers, "body": text}
        if self.metrics is not None:
            self.metrics.observe(ms, st, headers, text)
        return st, text

    def reset(self, section=""):
        return self._do("POST", "/__reset")

    def msg(self, m, section=""):
        return self._do("POST", "/msg", body={"m": m})

    def raw(self, method, path, body=None, section="", noauth=False):
        return self._do(method, path, raw=body, noauth=noauth)

    def count(self):
        return self.n

    def is_local(self):
        return True


OG = OrigTransport()

# ~60 banking-domain names (es + en) for probing new commands.
PROBE_NAMES = [
    "RETIRO", "RETIRAR", "SALDO", "SALDOS", "EXTRACTO", "MOVIMIENTOS", "HISTORIAL",
    "BLOQUEO", "BLOQUEAR", "DESBLOQUEO", "DESBLOQUEAR", "BAJA", "CIERRE", "CERRAR",
    "PAGO", "PAGAR", "TRANSFERENCIA", "TRANSFERIR", "COMISION", "LIMITE", "ESTADO",
    "STATUS", "VERSION", "HELP", "AYUDA", "LISTAR", "CUENTAS", "INTERES", "REVERSA",
    "REVERSAR", "CANCELA", "CANCELAR", "ABONO", "CARGO", "DEBITO", "CREDITO",
    "AJUSTE", "CONGELAR", "ACTIVAR", "DESACTIVAR", "RENOMBRAR", "ALIAS", "INFO",
    "PING2", "ECHO", "RESET", "SUMA", "TOTAL", "BALANCE", "WITHDRAW", "DEPOSIT",
    "QUERY", "OPEN", "CLOSE", "LOCK", "UNLOCK", "REVERSE", "CANCEL", "STATEMENT",
    "HISTORY",
]

# Near-miss at distance 1-2 from the real commands (#5): a typo/variant the
# service recognizes as "close" returns E301 with "sugerencia": <real command>,
# which reveals commands (including hidden ones like NOTAS).
NEAR_MISS = [
    "RETIRA", "ABONA", "CIERRA", "BLOQUEA", "HISTORIA", "MOVIMIENTO", "CONGELA",
    "AJUSTA", "DEPOSITA", "TRANSFIERE", "CONSUL", "ANULAR", "PINGS", "ALTAS",
    "NOTA", "HALTA",
]

# HALT is a REAL command (returns E302, not E301): it could have side effects
# (stopping the service), so it is NEVER sent.
EXCLUDE_NAMES = {"HALT"}

# Arity-0 probe over a known hidden command: the LITERAL BODY is compared.
NOTAS_PROBE = control.message("NOTAS")  # "NOTAS;11"

REAL_COMMANDS = ["PING", "ALTA", "CONSULTA", "DEPOSITO", "TRANSFER", "ANULA"]

# extra arguments to probe arity (real command -> valid args + 1 extra)
ARITY_OVERFLOW = {
    "PING": "PING;extra",
    "ALTA": "ALTA;Lucia;Gomez",
    "CONSULTA": "CONSULTA;AC-0001;extra",
    "DEPOSITO": "DEPOSITO;AC-0001;100;extra",
    "TRANSFER": "TRANSFER;AC-0001;AC-0002;100;extra",
    "ANULA": "ANULA;OP-0001;extra",
}

# Contract check (#1). body => compared by compare.py (exact body);
# error => only status + error code are compared (the "mensaje" may vary).
CONTRACT = [
    {"name": "health+token", "method": "GET", "path": "/health",
     "noauth": False, "status": 200, "body": '{"ok":true}'},
    {"name": "health-sin-token", "method": "GET", "path": "/health",
     "noauth": True, "status": 401, "error": "E001"},
    {"name": "reset-ok", "method": "POST", "path": "/__reset",
     "noauth": False, "status": 200, "body": '{"ok":true}'},
    {"name": "ruta-404", "method": "GET", "path": "/__noexiste",
     "noauth": False, "status": 404, "error": "E404"},
]


# --- Presupuesto diario persistente (#6) -----------------------------------
class DailyBudget:
    """In-memory budget for the run (--daily-budget) + a daily
    PERSISTENT counter of requests to the ORIGINAL in harness/budget.json. It
    only persists and enforces the 8000/day caps when the transport is the
    original (not local)."""
    def __init__(self, cap, transport, persist):
        self.cap = cap
        self.og = transport
        self.start = transport.count()
        self.persist = persist           # count against the daily budget?
        self.today = time.strftime("%Y-%m-%d")
        self.data = self._load()
        self.base_today = self.data.get(self.today, 0)
        self.stopped = False
        self._warned = False

    def _load(self):
        try:
            return json.load(open(BUDGET_FILE))
        except (OSError, ValueError):
            return {}

    def session_spent(self):
        return self.og.count() - self.start

    def spent(self):
        return self.session_spent()

    def remaining(self):
        return self.cap - self.spent()

    def ok(self):
        return self.spent() < self.cap and not self.stopped

    def daily_total(self):
        return self.base_today + self.session_spent()

    def save(self):
        if not self.persist:
            return
        self.data[self.today] = self.daily_total()
        try:
            json.dump(self.data, open(BUDGET_FILE, "w"), ensure_ascii=False, indent=0)
        except OSError:
            pass

    def enforce_daily(self):
        """Warn at 70%, stop at 90%. Return True if it must stop."""
        if not self.persist:
            return False
        tot = self.daily_total()
        frac = tot / DAILY_CAP if DAILY_CAP else 0.0
        if frac >= BUDGET_STOP:
            self.stopped = True
            drift(f"[budget] STOP: {tot}/{DAILY_CAP} ({frac*100:.0f}%) "
                  f">= {BUDGET_STOP*100:.0f}% of the original's daily cap")
            return True
        if frac >= BUDGET_WARN and not self._warned:
            self._warned = True
            drift(f"[budget] WARNING: {tot}/{DAILY_CAP} ({frac*100:.0f}%) "
                  f">= {BUDGET_WARN*100:.0f}% of the original's daily cap")
        return False

    def line(self):
        if self.persist:
            return (f"gasto diario original={self.daily_total()}/{DAILY_CAP} "
                    f"({self.daily_total()/DAILY_CAP*100:.0f}%); corrida={self.spent()}")
        return f"requests locales (no cuentan al presupuesto del original)={self.spent()}"


def drift(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line)
    with open(DRIFT_LOG, "a") as f:
        f.write(line + "\n")


def write_metric(target, rm, budget):
    """Write ONE line to harness/metrics.jsonl per round."""
    p50, p99, mx = rm.pcts()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target": target,
        "pasos": rm.steps,
        "iguales": rm.equal,
        "p50": p50,
        "p99": p99,
        "max": mx,
        "n_lentas": rm.n_slow,
        "n_5xx": rm.n_5xx,
        "n_infra": rm.n_infra,
        "requests_gastados": rm.req,
        "alertas": rm.alerts,
        "presupuesto_restante": budget.remaining(),
    }
    with open(METRICS_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load_probe_names():
    names = list(PROBE_NAMES)
    for n in NEAR_MISS:
        if n not in names:
            names.append(n)
    if os.path.exists(SONDAS_FILE):
        try:
            data = json.load(open(SONDAS_FILE))
            extra = data if isinstance(data, list) else data.get("nombres", [])
            for n in extra:
                if n not in names:
                    names.append(n)
            drift(f"[probes] {SONDAS_FILE} contributes {len(extra)} extra names")
        except (ValueError, OSError) as e:
            drift(f"[probes] could not read {SONDAS_FILE}: {e}")
    # HALT is never probed (a real command with possible side effects).
    return [n for n in names if n not in EXCLUDE_NAMES]


def load_battery_expected():
    with open(BATTERY_FILE) as f:
        case = json.loads(f.readline())
    return case["steps"]


def err_code(body):
    try:
        o = json.loads(body)
        return o.get("error") if isinstance(o, dict) else None
    except (ValueError, TypeError):
        return None


def suggestion_of(body):
    try:
        o = json.loads(body)
        return o.get("sugerencia") if isinstance(o, dict) else None
    except (ValueError, TypeError):
        return None


def last_is_infra():
    """Was the transport's LAST response from the infra (not the service)?"""
    lst = OG.last or {}
    return is_infra(lst.get("status"), lst.get("headers"), lst.get("body"))


# --- Chequeo de contrato (#1) ----------------------------------------------
def _contract_once(section):
    out = []
    for c in CONTRACT:
        status, body = OG.raw(c["method"], c["path"], section=section,
                              noauth=c.get("noauth", False))
        out.append({"c": c, "status": status, "body": body, "infra": last_is_infra()})
    return out


def _contract_diffs(obs):
    diffs = []
    for row in obs:
        if row["infra"]:
            continue  # respuesta de la infra: no es cambio del servicio
        c = row["c"]
        if "body" in c:
            ok, reason = C.compare({"status": c["status"], "body": c["body"]},
                                   {"status": row["status"], "body": row["body"]})
        else:
            same = (row["status"] == c["status"]) and (err_code(row["body"]) == c["error"])
            ok = same
            reason = (f"esperado {c['status']}/{c['error']}, "
                      f"observado {row['status']}/{err_code(row['body'])}")
        if not ok:
            diffs.append((c, row, reason))
    return diffs


def check_contract(rm):
    obs = _contract_once("monitor-contrato")
    rm.steps += len(CONTRACT)
    diffs = _contract_diffs(obs)
    rm.equal += len(CONTRACT) - len(diffs)
    if not diffs:
        drift(f"[contract] OK ({len(CONTRACT)} H/B checks equal)")
        return
    drift(f"[contract] {len(diffs)} provisional diffs; re-confirming...")
    obs2 = _contract_once("monitor-contrato")
    persist = _contract_diffs(obs2)
    if not persist:
        drift("[contract] the diffs did NOT persist (noise/transient infra)")
        return
    for c, row, reason in persist:
        rm.alerts += 1
        drift("CONTRACT ALERT [H/B class] check=%s %s %s\n   %s"
              % (c["name"], c["method"], c["path"], reason))


def run_battery_once(section):
    OG.reset(section=section)
    out = []
    for kind, payload, rule in L.BATTERY:
        m = L.build_message(kind, payload)
        status, body = OG.msg(m, section=section)
        out.append({"m": m, "rule": rule, "status": status, "body": body,
                    "infra": last_is_infra()})
    return out


def _battery_diffs(expected, obs):
    """Return a list of (idx, exp, ob, reason) ignoring steps marked INFRA."""
    diffs = []
    for idx, (exp, ob) in enumerate(zip(expected, obs)):
        if ob.get("infra"):
            continue
        ok, reason = C.compare({"status": exp["status"], "body": exp["body"]},
                               {"status": ob["status"], "body": ob["body"]})
        if not ok:
            diffs.append((idx, exp, ob, reason))
    return diffs


def check_battery(budget, rm, noisy=None):
    """Battery + re-confirmation. `noisy` = set of indices that require 3
    confirmations (instead of 2) for having been marked noisy (#3)."""
    noisy = noisy or set()
    expected = load_battery_expected()
    obs = run_battery_once("monitor")
    rm.steps += len(expected)
    diffs = _battery_diffs(expected, obs)
    rm.equal += len(expected) - len(diffs)
    if not diffs:
        drift(f"[battery] OK ({len(obs)} steps equal)")
        return
    drift(f"[battery] {len(diffs)} provisional diffs; re-confirming with reset...")
    # number of extra confirmations: 2 (noisy) or 1 (normal) per index.
    pending = {idx: (2 if idx in noisy else 1) for idx, *_ in diffs}
    detail = {idx: (exp, ob, reason) for idx, exp, ob, reason in diffs}
    confirmed = dict(pending)
    rounds_needed = max(pending.values())
    for _ in range(rounds_needed):
        if not budget.ok():
            drift("[battery] budget exhausted, no more re-confirmation")
            return
        obs_r = run_battery_once("monitor")
        still = {idx for idx, *_ in _battery_diffs(expected, obs_r)}
        for idx in list(pending):
            if idx not in still:
                confirmed.pop(idx, None)       # did not persist -> noise
                pending.pop(idx, None)
            else:
                pending[idx] -= 1
                if pending[idx] <= 0:
                    pending.pop(idx, None)
    if not confirmed:
        drift("[battery] the diffs did NOT persist the required confirmations (noise)")
        return
    for idx in sorted(confirmed):
        exp, ob, reason = detail[idx]
        rm.alerts += 1
        extra = " [reconfirmado x3, paso ruidoso]" if idx in noisy else ""
        drift("DRIFT ALERT [battery]%s rule=%s\n   message : %s\n   expected: %s %s\n   observed: %s %s"
              % (extra, exp.get("rule"), exp["m"], exp["status"], exp["body"],
                 ob["status"], ob["body"]))


# --- Piso de ruido control-vs-control (#3) ---------------------------------
def measure_noise(n):
    """Run the battery n times and return the set of step indices that differ
    between runs (control-vs-control; should be empty)."""
    runs = []
    for _ in range(n):
        runs.append(run_battery_once("monitor-ruido"))
    noisy = set()
    base = runs[0]
    for other in runs[1:]:
        for idx, (a, b) in enumerate(zip(base, other)):
            if a.get("infra") or b.get("infra"):
                continue
            ok, _ = C.compare({"status": a["status"], "body": a["body"]},
                              {"status": b["status"], "body": b["body"]})
            if not ok:
                noisy.add(idx)
    return noisy


def build_cmd_baseline(budget):
    names = load_probe_names()
    OG.reset(section="monitor-baseline-cmds")
    OG.msg(control.message("ALTA;Base"), section="monitor-baseline-cmds")
    rows = []
    sugerencias = set()
    for nm in names:
        if not budget.ok():
            drift("[baseline] budget exhausted")
            break
        msg = control.message(f"{nm};AC-0001")
        status, body = OG.msg(msg, section="monitor-baseline-cmds")
        sug = suggestion_of(body)
        if sug:
            sugerencias.add(sug)
        rows.append({"nombre": nm, "m": msg, "status": status, "body": body,
                     "error": err_code(body), "sugerencia": sug})
    # arity-0 probe NOTAS;11, literal body.
    st, bd = OG.msg(NOTAS_PROBE, section="monitor-baseline-cmds")
    notas = {"nombre": "__NOTAS_ARIDAD0__", "m": NOTAS_PROBE, "status": st, "body": bd}
    with open(CMD_BASELINE, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
        f.write(json.dumps(notas, ensure_ascii=False) + "\n")
        f.write(json.dumps({"nombre": "__SUGERENCIAS__",
                            "set": sorted(sugerencias)}, ensure_ascii=False) + "\n")
    existing = [r["nombre"] for r in rows if r["error"] != "E301"]
    drift(f"[baseline] {len(rows)} commands -> {os.path.relpath(CMD_BASELINE, ROOT)}; "
          f"already existing (not E301): {existing or 'none'}; "
          f"E301 suggestions: {sorted(sugerencias) or 'none'}")
    return rows


def discovery_hint(name):
    return ("suggested discovery sequence for %s:\n"
            "   1) arity 0..4: %s;<c>, %s;AC-0001;<c>, %s;AC-0001;100;<c>, "
            "%s;AC-0001;100;OP-0001;<c> (E302 vs something else)\n"
            "   2) typical arguments: AC-0001, amounts (0/100/100000/9999999999), OP-0001\n"
            "   3) CONSULTA;AC-0001 before and after to see the effect on the ledger"
            % (name, name, name, name, name))


def probe_new_commands(budget, rm):
    if not os.path.exists(CMD_BASELINE):
        drift("[probe-cmds] no baseline; building it now")
        build_cmd_baseline(budget)
        return
    base = {}
    notas_base = None
    base_sugs = set()
    for line in open(CMD_BASELINE):
        r = json.loads(line)
        if r.get("nombre") == "__NOTAS_ARIDAD0__":
            notas_base = r
        elif r.get("nombre") == "__SUGERENCIAS__":
            base_sugs = set(r.get("set", []))
        else:
            base[r["nombre"]] = r
    OG.reset(section="monitor-sondeo-cmds")
    OG.msg(control.message("ALTA;Base"), section="monitor-sondeo-cmds")
    seen_sugs = set()
    for nm, br in base.items():
        if not budget.ok():
            drift("[probe-cmds] budget exhausted")
            break
        msg = control.message(f"{nm};AC-0001")
        status, body = OG.msg(msg, section="monitor-sondeo-cmds")
        if last_is_infra():
            continue
        rm.steps += 1
        sug = suggestion_of(body)
        if sug:
            seen_sugs.add(sug)
        now_err = err_code(body)
        ok, reason = C.compare({"status": br["status"], "body": br["body"]},
                               {"status": status, "body": body})
        if ok:
            rm.equal += 1
            continue
        rm.alerts += 1
        drift("PROBE-CMD ALERT %s changed\n   base: %s %s\n   now : %s %s (%s)"
              % (nm, br["status"], br["body"], status, body, reason))
        if br.get("error") == "E301" and now_err != "E301":
            drift("[probe-cmds] %s stopped returning E301 -> possible NEW command" % nm)
            drift(discovery_hint(nm))
    # New E301 suggestions = new HIDDEN command (#5).
    nuevas = seen_sugs - base_sugs
    if nuevas:
        rm.alerts += 1
        drift("PROBE-CMD ALERT NEW E301 suggestion(s): %s -> new hidden command(s)"
              % sorted(nuevas))
    # Arity-0 probe NOTAS;11, LITERAL body (#5).
    if notas_base is not None and budget.ok():
        st, bd = OG.msg(NOTAS_PROBE, section="monitor-sondeo-cmds")
        if not last_is_infra():
            rm.steps += 1
            if st == notas_base["status"] and bd == notas_base["body"]:  # literal
                rm.equal += 1
            else:
                rm.alerts += 1
                drift("PROBE-CMD ALERT NOTAS;11 (arity 0) changed the LITERAL body\n"
                      "   base: %s %r\n   now : %s %r"
                      % (notas_base["status"], notas_base["body"], st, bd))


def probe_arity(budget, rm):
    OG.reset(section="monitor-sondeo-aridad")
    OG.msg(control.message("ALTA;Base"), section="monitor-sondeo-aridad")
    for cmd in REAL_COMMANDS:
        if not budget.ok():
            drift("[probe-arity] budget exhausted")
            return
        msg = control.message(ARITY_OVERFLOW[cmd])
        status, body = OG.msg(msg, section="monitor-sondeo-aridad")
        if last_is_infra():
            continue
        rm.steps += 1
        if err_code(body) != "E302":
            rm.alerts += 1
            drift("PROBE-ARITY ALERT %s with one extra arg NO longer returns E302 -> %s %s"
                  % (cmd, status, body))
        else:
            rm.equal += 1


def do_round(budget, is_long, idx, target, noisy, infra_wait):
    drift(f"=== round {idx} ({'LONG' if is_long else 'short'}) {budget.line()} ===")
    rm = RoundMetrics()
    OG.metrics = rm
    try:
        check_contract(rm)
        check_battery(budget, rm, noisy=noisy)
        if is_long:
            probe_new_commands(budget, rm)
            probe_arity(budget, rm)
    finally:
        OG.metrics = None
    # infra retry: if there were infra responses, wait and re-observe
    if rm.n_infra and infra_wait > 0:
        drift(f"[infra] {rm.n_infra} INFRA response(s) this round; "
              f"retry after {infra_wait}s")
        time.sleep(infra_wait)
    p50, p99, mx = rm.pcts()
    drift(f"[latency] p50={p50}ms p99={p99}ms max={mx}ms "
          f"slow>5s={rm.n_slow} 5xx={rm.n_5xx} infra={rm.n_infra} "
          f"req={rm.req} steps={rm.steps} equal={rm.equal} alerts={rm.alerts}")
    rec = write_metric(target, rm, budget)
    drift(f"[metrics] {os.path.relpath(METRICS_FILE, ROOT)} <- "
          f"steps={rec['pasos']} equal={rec['iguales']} alerts={rec['alertas']}")
    budget.save()
    return rm


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=180)
    ap.add_argument("--long-every", type=int, default=6)
    ap.add_argument("--rounds", type=int, default=1, help="0 = infinite; default 1 (one round)")
    ap.add_argument("--once", action="store_true", help="exactly one round and exit")
    ap.add_argument("--long", action="store_true", help="force the round to be long")
    ap.add_argument("--daily-budget", type=int, default=2000,
                    help="request cap for THIS run")
    ap.add_argument("--build-baseline", action="store_true")
    ap.add_argument("--corpus", default=None,
                    help="corpus directory whose battery.jsonl is the baseline (default corpus/; use corpus-v2 after a rule change)")
    ap.add_argument("--noise", type=int, default=0,
                    help="N battery runs at the start to measure the control-vs-control "
                         "noise floor (#3); noisy steps require 3 confirmations")
    ap.add_argument("--infra-wait", type=int, default=10,
                    help="seconds to wait after detecting INFRA responses (#4)")
    ap.add_argument("--target", default=None,
                    help="URL of a LOCAL emulator; without it the original is used via cn.py")
    ap.add_argument("--token", default="lab", help="Bearer token for --target")
    args = ap.parse_args()

    global OG, CMD_BASELINE, BATTERY_FILE
    if args.corpus:
        BATTERY_FILE = os.path.join(args.corpus if os.path.isabs(args.corpus) else os.path.join(ROOT, args.corpus), "battery.jsonl")
    target_label = "ORIGINAL"
    persist_budget = True
    if args.target:
        from urllib.parse import urlparse
        if urlparse(args.target).netloc.startswith("caja-negra"):
            print("[monitor] REJECTED: --target looks like the original; forbidden.", file=sys.stderr)
            sys.exit(3)
        OG = TargetTransport(args.target, args.token)
        persist_budget = False  # local: does not consume the original's daily budget
        label = urlparse(args.target).netloc.replace(":", "_")
        target_label = args.target
        CMD_BASELINE = os.path.join(ROOT, "lab", f"commands_baseline_{label}.jsonl")
        os.makedirs(os.path.join(ROOT, "lab"), exist_ok=True)
        drift(f"[monitor] LOCAL target = {args.target} (token={args.token})")

    budget = DailyBudget(args.daily_budget, OG, persist_budget)
    if budget.enforce_daily():
        drift("[monitor] original's daily budget already at 90%; not starting")
        return

    if args.build_baseline:
        rm = RoundMetrics()
        OG.metrics = rm
        build_cmd_baseline(budget)
        OG.metrics = None
        budget.save()
        drift(f"[monitor] requests spent: {budget.spent()}; {budget.line()}")
        return

    noisy = set()
    if args.noise > 0:
        rm0 = RoundMetrics()
        OG.metrics = rm0
        noisy = measure_noise(args.noise)
        OG.metrics = None
        budget.save()
        if noisy:
            drift(f"[noise] {len(noisy)} NOISY step(s) across {args.noise} control-vs-control "
                  f"runs: indices {sorted(noisy)} -> will require 3 confirmations")
        else:
            drift(f"[noise] noise floor = 0 ({args.noise} identical runs)")

    rounds = 1 if args.once else args.rounds
    idx = 0
    try:
        while True:
            idx += 1
            is_long = args.long or (args.long_every > 0 and idx % args.long_every == 0)
            if not budget.ok():
                drift("[monitor] run budget exhausted; stopping")
                break
            if budget.enforce_daily():
                break
            do_round(budget, is_long, idx, target_label, noisy, args.infra_wait)
            if rounds and idx >= rounds:
                break
            time.sleep(args.interval)
    finally:
        budget.save()
        drift(f"[monitor] done. requests spent: {budget.spent()}; {budget.line()}")


if __name__ == "__main__":
    main()

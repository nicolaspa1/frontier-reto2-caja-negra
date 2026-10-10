#!/usr/bin/env python3
"""Independent hidden-set-shaped generator for Reto 2 (validator's own oracle).

Does NOT read corpus/*.jsonl. Expectations come from a small simulator written
from the rules documented in spec/especificacion.md (see also docs/ARCHITECTURE.md).
For every step the generator then searches experiments/log.jsonl for a line with
the SAME request and the SAME (status, body); if found the step is tagged with
that line ("L<n>"), otherwise "derived" (rule-derived, never observed verbatim).

Output: validation/hidden-like.jsonl, one case per line:
  {"case_id": ..., "kind": "single|short|long", "steps": [
      {"m": "...", "expected_status": 200, "expected_body": "...", "source": "L2"},
      {"raw": {"method","path","body","auth","headers"}, "expected_status":..., ...}]}
Shape: 40 x 1 step, 45 x 2-5 steps, 15 x 6-12 steps (= 100 cases).
"""
import json
import os
import random
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG = os.path.join(ROOT, "experiments", "log.jsonl")
OUT = os.path.join(ROOT, "validation", "hidden-like.jsonl")

# ----------------------------------------------------------------- oracle ---
KNOWN = ["PING", "ALTA", "CONSULTA", "DEPOSITO", "TRANSFER", "ANULA", "HALT", "NOTAS"]
ARITY = {"PING": 0, "ALTA": 1, "CONSULTA": 1, "DEPOSITO": 2, "TRANSFER": 3, "ANULA": 1,
         "HALT": 1, "NOTAS": 0}
ERR = {
    "E001": (401, "no autorizado"), "E100": (400, "json inválido"), "E101": (413, "cuerpo demasiado grande"),
    "E102": (400, "falta m"), "E103": (400, "falta control"), "E104": (400, "segmento vacío"),
    "E201": (400, "control mal formado"), "E202": (400, "control inválido"),
    "E301": (400, "comando desconocido "), "E302": (400, "aridad inválida"), "E303": (400, "monto inválido"),
    "E304": (400, "origen y destino iguales"), "E401": (404, "cuenta inexistente"),
    "E402": (404, "operación inexistente"), "E404": (404, "ruta desconocida"),
    "E506": (422, "límite excedido"), "E507": (422, "saldo insuficiente"), "E510": (409, "cuenta bloqueada"),
    "E520": (409, "operación destructiva: detiene el servicio; reenvía con confirmación"),
}
TRANSFER_MAX = 100000
AMOUNT_RE = re.compile(r"^[1-9][0-9]{0,9}$")       # 1..9999999999, no sign, no leading zero
CTRL_RE = re.compile(r"^[0-9]{1,2}$")
BODY_MAX = 8192


def J(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def err(code, extra=None):
    st, msg = ERR[code]
    o = {"error": code, "mensaje": msg}
    if extra:
        o.update(extra)
    return st, J(o)


def control(payload):
    """ISO 7064 MOD 97-10 over 'CMD;arg;...': A..Z=1..26 (case-insensitive), digits kept,
    everything else dropped, append 00, control = 98 - (N mod 97), sent without leading zero."""
    digits = ""
    for ch in payload:
        if ch.isascii() and ch.isalpha():
            digits += str(ord(ch.upper()) - 64)
        elif ch.isascii() and ch.isdigit():
            digits += ch
    n = int(digits + "00") if digits else 0
    return str(98 - (n % 97))


def msg(payload):
    return f"{payload};{control(payload)}"


def lev(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def suggestion(name):
    best = [(lev(name, c), i, c) for i, c in enumerate(KNOWN)]
    best.sort()
    d, _, c = best[0]
    ties = [x for x in best if x[0] == d]
    return (c if d <= 2 else None), (len(ties) > 1 and d <= 2)


class Oracle:
    def __init__(self, notas_body):
        self.notas_body = notas_body
        self.reset()

    def reset(self):
        self.acc = {}       # id -> balance
        self.ops = {}       # id -> dict(kind, args, anulada)
        self.next_ac = 1
        self.next_op = 1
        self.strikes = {}
        self.blocked = set()

    # ---- message pipeline (valid auth, valid envelope)
    def process(self, m):
        if ";" not in m:
            return err("E103")
        segs = m.split(";")
        if any(s == "" for s in segs):
            return err("E104")
        cmd, args, ctrl = segs[0].upper(), segs[1:-1], segs[-1]
        if cmd != "PING":
            if not CTRL_RE.match(ctrl):
                return err("E201")
            if ctrl != control(";".join(segs[:-1])):
                return err("E202")
        if cmd not in KNOWN:
            sug, _ = suggestion(cmd)
            return err("E301", {"sugerencia": sug} if sug else None)
        if len(args) != ARITY[cmd]:
            return err("E302")
        return getattr(self, "cmd_" + cmd.lower())(args)

    def cmd_ping(self, a):
        return 200, J({"pong": True})

    def cmd_notas(self, a):
        return 200, self.notas_body

    def cmd_halt(self, a):
        return err("E520")

    def cmd_alta(self, a):
        cid = f"AC-{self.next_ac:04d}"
        self.next_ac += 1
        self.acc[cid] = 0
        self.strikes[cid] = 0
        return 201, J({"cuentaId": cid})

    def cmd_consulta(self, a):
        if a[0] not in self.acc:
            return err("E401")
        return 200, J({"cuenta_id": a[0], "saldo": self.acc[a[0]]})

    def _op(self, kind, **kw):
        oid = f"OP-{self.next_op:04d}"
        self.next_op += 1
        self.ops[oid] = dict(kind=kind, anulada=False, **kw)
        return 200, J({"status": "recieved", "operacionId": oid})

    def cmd_deposito(self, a):
        cta, monto = a
        if not AMOUNT_RE.match(monto):
            return err("E303")
        if cta not in self.acc:
            return err("E401")
        self.acc[cta] += int(monto)
        return self._op("DEPOSITO", cta=cta, monto=int(monto))

    def cmd_transfer(self, a):
        o, d, monto = a
        if not AMOUNT_RE.match(monto):
            return err("E303")
        if o == d:
            return err("E304")
        if o not in self.acc or d not in self.acc:
            return err("E401")
        if o in self.blocked:
            return err("E510")
        n = int(monto)
        if n > TRANSFER_MAX:
            return self._strike(o, "E506")
        if self.acc[o] < n:
            return self._strike(o, "E507")
        self.acc[o] -= n
        self.acc[d] += n
        self.strikes[o] = 0
        return self._op("TRANSFER", o=o, d=d, monto=n)

    def _strike(self, o, code):
        self.strikes[o] = self.strikes.get(o, 0) + 1
        if self.strikes[o] >= 3:
            self.blocked.add(o)
        return err(code)

    def cmd_anula(self, a):
        op = self.ops.get(a[0])
        if op is None:
            return err("E402")
        if op["anulada"]:
            return 200, J({"ok": False, "motivo": "ya anulada"})
        op["anulada"] = True
        if op["kind"] == "DEPOSITO":
            self.acc[op["cta"]] -= op["monto"]
        else:
            self.acc[op["o"]] += op["monto"]
            self.acc[op["d"]] -= op["monto"]
        return 200, J({"ok": True, "anulada": a[0]})

    # ---- HTTP layer. auth in {"valid","none","lowercase","wrong","noscheme"}
    def raw(self, method, path, body, auth="valid"):
        p = path.split("?", 1)[0]
        route = (method.upper(), p)
        if route not in {("POST", "/msg"), ("POST", "/__reset"), ("GET", "/health")}:
            return err("E404")
        if auth != "valid":
            return err("E001")
        if route == ("POST", "/__reset"):
            self.reset()
            return 200, J({"ok": True})
        if route == ("GET", "/health"):
            return 200, J({"ok": True})
        b = body if body is not None else ""
        if len(b.encode("utf-8")) > BODY_MAX:
            return err("E101")
        try:
            o = json.loads(b) if b != "" else None
        except ValueError:
            return err("E100")
        if o is None and b == "":
            return err("E100")
        if not isinstance(o, dict) or not isinstance(o.get("m"), str):
            return err("E102")
        return self.process(o["m"])


# ------------------------------------------------------------ evidence ---
def load_log():
    rows = []
    for line in open(LOG, encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def sem(body):
    try:
        return ("json", json.loads(body))
    except (ValueError, TypeError):
        return ("text", body)


def norm_body(b):
    """Raw bodies compare semantically when they are JSON (the log stores dicts re-serialized)."""
    if b is None:
        return None
    try:
        return json.dumps(json.loads(b), sort_keys=True, ensure_ascii=False)
    except (ValueError, TypeError):
        return b


class Evidence:
    def __init__(self, rows):
        self.by_m = {}
        self.by_raw = {}
        self.by_bytes = {}
        for r in rows:
            req, res = r["req"], r["res"]
            key_res = (res["status"], json.dumps(sem(res["body"]), sort_keys=True, ensure_ascii=False))
            body = req.get("body")
            if req["method"] == "POST" and req["path"] == "/msg" and isinstance(body, dict) and isinstance(body.get("m"), str) \
                    and req.get("auth_kind", "valid") == "valid":
                self.by_m.setdefault((body["m"], key_res), r["n"])
            else:
                bstr = body if isinstance(body, str) else (None if body is None else json.dumps(body))
                if "body_bytes" in req and isinstance(bstr, str) and bstr.startswith("<cuerpo"):
                    self.by_bytes.setdefault((req["body_bytes"], key_res), r["n"])
                self.by_raw.setdefault((req["method"], req["path"], norm_body(bstr), req.get("auth_kind", "valid"), key_res), r["n"])

    def find_m(self, m, status, body):
        n = self.by_m.get((m, (status, json.dumps(sem(body), sort_keys=True, ensure_ascii=False))))
        return f"L{n}" if n else "derived"

    def find_raw(self, method, path, body, auth, status, exp_body, nbytes=None):
        key_res = (status, json.dumps(sem(exp_body), sort_keys=True, ensure_ascii=False))
        kinds = {"valid": ["valid"], "none": ["none", "valid"], "lowercase": ["lowercase-bearer", "valid"],
                 "wrong": ["wrong", "valid"], "noscheme": ["noscheme", "valid"]}[auth]
        # 'valid' fallback: lines 151-196 did not record auth_kind (logged as valid by default)
        for k in kinds:
            n = self.by_raw.get((method, path, norm_body(body), k, key_res))
            if n:
                return f"L{n}"
        if nbytes is not None:
            n = self.by_bytes.get((nbytes, key_res))
            if n:
                return f"L{n}"
        return "derived"


# ------------------------------------------------------------ builder ---
class Builder:
    def __init__(self, oracle, ev):
        self.o, self.ev = oracle, ev
        self.cases = []

    def case(self, cid, kind, steps):
        """steps: list of ('m', text) | ('raw', dict(method,path,body,auth,headers?,nbytes?))"""
        self.o.reset()
        out = []
        for s in steps:
            if s[0] == "m":
                st, body = self.o.raw("POST", "/msg", J({"m": s[1]}))
                out.append({"m": s[1], "expected_status": st, "expected_body": body,
                            "source": self.ev.find_m(s[1], st, body)})
            else:
                r = dict(s[1])
                auth = r.get("auth", "valid")
                st, body = self.o.raw(r["method"], r["path"], r.get("body"), auth)
                src = self.ev.find_raw(r["method"], r["path"], r.get("body"), auth, st, body, r.get("nbytes"))
                raw = {"method": r["method"], "path": r["path"], "body": r.get("body"), "auth": auth}
                if r.get("headers"):
                    raw["headers"] = r["headers"]
                out.append({"raw": raw, "expected_status": st, "expected_body": body, "source": src})
        self.cases.append({"case_id": cid, "kind": kind, "steps": out})


def M(payload):
    return ("m", msg(payload))


def R(method, path, body=None, auth="valid", **kw):
    d = {"method": method, "path": path, "body": body, "auth": auth}
    d.update(kw)
    return ("raw", d)


def big_body(nbytes):
    prefix, suffix = '{"m":"', '"}'
    return prefix + "A" * (nbytes - len(prefix) - len(suffix)) + suffix


def main():
    rows = load_log()
    notas = next(r["res"]["body"] for r in rows if r["req"].get("body") == {"m": "NOTAS;11"} and r["res"]["status"] == 200)
    o = Oracle(notas)
    ev = Evidence(rows)
    b = Builder(o, ev)

    # sanity: control algorithm vs official examples
    assert control("PING") == "64" and control("ALTA;Lucia") == "82" and control("CONSULTA;AC-0001") == "14"
    assert control("DEPOSITO;AC-0001;250000") == "86" and control("ALTA;Mateo") == "3"
    assert suggestion("HELP") == ("HALT", False) and suggestion("AYUDA") == ("ANULA", False)

    # ------------------------------------------------------------ singles (40)
    singles = [
        ("s01-ping", [("m", "PING;64")]),
        ("s02-ping-xx-control-ignored", [("m", "PING;XX")]),
        ("s03-ping-lowercase", [("m", "ping;64")]),
        ("s04-ping-no-control", [("m", "PING")]),
        ("s05-ping-empty-control", [("m", "PING;")]),
        ("s06-ping-arity", [("m", "PING;64;64")]),
        ("s07-notas", [("m", "NOTAS;11")]),
        ("s08-halt-arity0", [("m", "HALT;71")]),
        ("s09-consulta-no-accounts", [M("CONSULTA;AC-0001")]),
        ("s10-alta", [M("ALTA;Lucia")]),
        ("s11-alta-leading-zero-control", [("m", "ALTA;Mateo;03")]),
        ("s12-alta-wrong-control", [("m", "ALTA;Lucia;83")]),
        ("s13-alta-malformed-control", [("m", "ALTA;Lucia;XX")]),
        ("s14-control-00", [("m", "CONSULTA;AC-0001;00")]),
        ("s15-control-3digits", [("m", "CONSULTA;AC-0001;014")]),
        ("s16-alta-empty-name", [("m", "ALTA;;85")]),
        ("s17-unknown-cmd", [("m", "SALDOS;AC-0001;38")]),
        ("s18-unknown-with-suggestion-HELP", [("m", "HELP;72")]),
        ("s19-unknown-with-suggestion-TRANSFERIR", [("m", "TRANSFERIR;AC-0001;61")]),
        ("s20-unknown-bad-control-E201-first", [("m", "NOEXISTE;XX")]),
        ("s21-unknown-wrong-control-E202-first", [("m", "NOEXISTE;AC-0001;99")]),
        ("s22-arity-vs-control-E202", [("m", "ALTA;Lucia;Gomez;99")]),
        ("s23-arity-E302", [("m", "ALTA;Lucia;Gomez;11")]),
        ("s24-deposito-amount-0", [("m", "DEPOSITO;AC-0001;0;31")]),
        ("s25-deposito-arity3", [("m", "DEPOSITO;AC-0001;100;extra;91")]),
        ("s26-transfer-same-acct-amount0-E303", [("m", "TRANSFER;AC-0001;AC-0001;0;74")]),
        ("s27-transfer-same-nonexistent-E304", [("m", "TRANSFER;AC-0099;AC-0099;100;19")]),
        ("s28-transfer-no-accounts-E401", [M("TRANSFER;AC-0001;AC-0002;100000")]),
        ("s29-anula-no-ops", [("m", "ANULA;OP-0001;72")]),
        ("s30-no-separator-E103", [("m", "HOLA")]),
        ("s31-empty-m", [("m", "")]),
        ("s32-health-no-token", [R("GET", "/health", auth="none")]),
        ("s33-root-404", [R("GET", "/")]),
        ("s34-post-health-404", [R("POST", "/health")]),
        ("s35-get-reset-404", [R("GET", "/__reset")]),
        ("s36-msg-trailing-slash-404", [R("POST", "/msg/", J({"m": "PING;64"}))]),
        ("s37-invalid-json-E100", [R("POST", "/msg", "{")]),
        ("s38-m-not-string-E102", [R("POST", "/msg", '{"m":123}')]),
        ("s39-body-8193-E101", [R("POST", "/msg", big_body(8193), nbytes=8193)]),
        ("s40-lowercase-bearer-401", [R("POST", "/msg", J({"m": "PING;64"}), auth="lowercase")]),
    ]
    for cid, steps in singles:
        b.case(cid, "single", steps)

    # ------------------------------------------------------- short (45): 2-5 steps
    short = [
        ("h01-limit-boundary", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0001;1000000"),
                                M("TRANSFER;AC-0001;AC-0002;100000"), M("TRANSFER;AC-0001;AC-0002;100001")]),
        ("h02-amount-max", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;9999999999"), M("DEPOSITO;AC-0001;10000000000"),
                            M("CONSULTA;AC-0001")]),
        ("h03-anula-twice", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;5000"), M("ANULA;OP-0001"), M("ANULA;OP-0001"),
                             M("CONSULTA;AC-0001")]),
        ("h04-op-not-consumed-by-failures", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0003;100"),
                                             M("TRANSFER;AC-0001;AC-0002;100"), M("DEPOSITO;AC-0001;100")]),
        ("h05-halt-with-account", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;5000"), M("HALT;AC-0001"),
                                   M("CONSULTA;AC-0001"), M("DEPOSITO;AC-0001;10")]),
        ("h06-auth-does-not-mutate", [M("ALTA;Lucia"), R("POST", "/msg", J({"m": msg("ALTA;Mateo")}), auth="none"),
                                      R("POST", "/__reset", auth="wrong"), M("CONSULTA;AC-0002"), M("CONSULTA;AC-0001")]),
        ("h07-leading-zero-amount", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;0100"), M("DEPOSITO;AC-0001;100"),
                                     M("CONSULTA;AC-0001")]),
        ("h08-reset-mid-sequence", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;700"), R("POST", "/__reset"),
                                    M("CONSULTA;AC-0001"), M("ALTA;Ana")]),
        ("h09-case-insensitive-cmd", [M("ALTA;Lucia"), ("m", "CONSULTa;AC-0001;14"), ("m", "deposito;AC-0001;100;82"),
                                      M("consulta;AC-0001")]),
        ("h10-ids-exact", [M("ALTA;Lucia"), M("CONSULTA;ac-0001"), M("CONSULTA;AC-1"), M("CONSULTA;AC-00001"),
                           M("CONSULTA;AC-0001")]),
        ("h11-e507-then-ok", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("TRANSFER;AC-0001;AC-0002;100"),
                              M("DEPOSITO;AC-0001;100"), M("TRANSFER;AC-0001;AC-0002;100")]),
        ("h12-e506-before-e507", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("TRANSFER;AC-0001;AC-0002;200000"),
                                  M("CONSULTA;AC-0001")]),
        ("h13-e401-before-e506", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;500000"), M("TRANSFER;AC-0001;AC-0099;200000")]),
        ("h14-e304-before-e506", [M("ALTA;Lucia"), M("TRANSFER;AC-0001;AC-0001;100001"), M("CONSULTA;AC-0001")]),
        ("h15-anula-transfer-restores", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0001;100000"),
                                         M("TRANSFER;AC-0001;AC-0002;60000"), M("ANULA;OP-0002")]),
        ("h16-anula-nonexistent", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;5"), M("ANULA;OP-9999"), M("ANULA;OP-0002")]),
        ("h17-query-string-and-content-type", [R("POST", "/msg?x=1", J({"m": "PING;64"})),
                                               R("POST", "/msg", J({"m": "PING;64"}), headers={"Content-Type": "text/plain"})]),
        ("h18-body-8192-vs-8193", [R("POST", "/msg", big_body(8192), nbytes=8192), R("POST", "/msg", big_body(8193), nbytes=8193)]),
        ("h19-envelope-errors", [R("POST", "/msg", ""), R("POST", "/msg", "{}"), R("POST", "/msg", "[1]"),
                                 R("POST", "/msg", '{"m":null}')]),
        ("h20-routes", [R("POST", "/MSG", J({"m": "PING;64"})), R("OPTIONS", "/msg"), R("GET", "/noexiste", auth="none"),
                        R("GET", "/health")]),
        ("h21-unknown-suggestions", [("m", "AYUDA;97"), M("ALIAS;AC-0001"), M("DEPOSIT;AC-0001"), M("SALDO;AC-0001")]),
        ("h22-ping-variants", [("m", "PING;1"), ("m", "PING;5X"), ("m", "PING;extra;88")]),
        ("h23-halt-control-validated", [M("ALTA;Lucia"), ("m", "HALT;AC-0001;99"), ("m", "HALT;AC-0001;1;32")]),
        ("h24-notas-arity", [("m", "NOTAS;AC-0001;14"), ("m", "NOTAS;11")]),
        ("h25-empty-segments", [M("ALTA;Lucia"), ("m", "DEPOSITO;AC-0001;;4"), ("m", "TRANSFER;AC-0001;;100;27"),
                                M("CONSULTA;AC-0001")]),
        ("h26-nonascii-name-control", [("m", "ALTA;José;61"), ("m", "ALTA;José;4"), M("CONSULTA;AC-0001")]),
        ("h27-amount-formats", [M("ALTA;Lucia"), M("DEPOSITO;AC-0001;-5"), M("DEPOSITO;AC-0001;10.5"),
                                M("DEPOSITO;AC-0001;abc"), M("CONSULTA;AC-0001")]),
        ("h28-transfer-amount-over-max", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("TRANSFER;AC-0001;AC-0002;10000000000"),
                                          M("TRANSFER;AC-0001;AC-0002;999999999999")]),
        ("h29-deposit-nonexistent-vs-amount", [M("DEPOSITO;AC-0001;100"), M("DEPOSITO;AC-0001;0"), M("ALTA;Lucia"),
                                               M("DEPOSITO;AC-0001;100")]),
        ("h30-arity-before-rules", [M("DEPOSITO;AC-0001"), M("TRANSFER;AC-0001;AC-0002"), M("ANULA;OP-0001;extra"),
                                    M("CONSULTA")]),
    ]
    for cid, steps in short:
        b.case(cid, "short", steps)

    # ------------------------------------------------------- long (15): 6-12 steps
    long_ = [
        ("l01-block-after-3", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0001;1000000"),
                               M("TRANSFER;AC-0001;AC-0002;100001"), M("TRANSFER;AC-0001;AC-0002;100001"),
                               M("TRANSFER;AC-0001;AC-0002;100001"), M("TRANSFER;AC-0001;AC-0002;100001"),
                               M("TRANSFER;AC-0001;AC-0002;100"), M("CONSULTA;AC-0002")]),
        ("l02-counter-reset-on-success", [M("ALTA;Origen"), M("ALTA;Destino"), M("DEPOSITO;AC-0001;9999999999"),
                                          M("TRANSFER;AC-0001;AC-0002;100001"), M("TRANSFER;AC-0001;AC-0002;100001"),
                                          M("TRANSFER;AC-0001;AC-0002;100000"), M("TRANSFER;AC-0001;AC-0002;100001"),
                                          M("TRANSFER;AC-0001;AC-0002;100001"), M("TRANSFER;AC-0001;AC-0002;100")]),
        ("l03-anula-negative-balance", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("ALTA;Ana"), M("DEPOSITO;AC-0001;100000"),
                                        M("TRANSFER;AC-0001;AC-0002;60000"), M("TRANSFER;AC-0002;AC-0003;60000"),
                                        M("ANULA;OP-0002"), M("CONSULTA;AC-0001"), M("CONSULTA;AC-0002"), M("CONSULTA;AC-0003")]),
        ("l04-anula-numbering", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0001;100000"),
                                 M("TRANSFER;AC-0001;AC-0002;60000"), M("ANULA;OP-0002"), M("CONSULTA;AC-0001"),
                                 M("CONSULTA;AC-0002"), M("ANULA;OP-0002"), M("ANULA;OP-0001"), M("CONSULTA;AC-0001"),
                                 M("ANULA;OP-9999"), M("DEPOSITO;AC-0001;5000")]),
        ("l05-e401-not-a-strike", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0001;500000"),
                                   M("TRANSFER;AC-0001;AC-0002;100001"), M("TRANSFER;AC-0001;AC-0099;200000"),
                                   M("TRANSFER;AC-0001;AC-0002;100001"), M("TRANSFER;AC-0001;AC-0002;100001"),
                                   M("TRANSFER;AC-0001;AC-0002;100")]),
        ("l06-blocked-still-receives", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("DEPOSITO;AC-0002;50000"),
                                        M("TRANSFER;AC-0001;AC-0002;100"), M("TRANSFER;AC-0001;AC-0002;100"),
                                        M("TRANSFER;AC-0001;AC-0002;100"), M("TRANSFER;AC-0001;AC-0002;100"),
                                        M("DEPOSITO;AC-0001;1000"), M("TRANSFER;AC-0002;AC-0001;500"),
                                        M("CONSULTA;AC-0001"), M("TRANSFER;AC-0001;AC-0002;1")]),
        ("l07-e510-order", [M("ALTA;Lucia"), M("ALTA;Mateo"), M("TRANSFER;AC-0001;AC-0002;1"),
                            M("TRANSFER;AC-0001;AC-0002;1"), M("TRANSFER;AC-0001;AC-0002;1"),
                            M("TRANSFER;AC-0001;AC-0002;120000"), M("TRANSFER;AC-0001;AC-0099;5"),
                            M("TRANSFER;AC-0001;AC-0001;5"), M("TRANSFER;AC-0001;AC-0002;0"), M("DEPOSITO;AC-0001;9")]),
        ("l08-no-accumulated-limit", [M("ALTA;A"), M("ALTA;B"), M("DEPOSITO;AC-0001;500000"),
                                      M("TRANSFER;AC-0001;AC-0002;60000"), M("TRANSFER;AC-0001;AC-0002;60000"),
                                      M("TRANSFER;AC-0001;AC-0002;60000"), M("CONSULTA;AC-0002"), M("CONSULTA;AC-0001")]),
    ]
    for cid, steps in long_:
        b.case(cid, "long", steps)

    # ------------------------------------------- randomized sequences from the oracle
    rng = random.Random(20261010)
    names = ["Lucia", "Mateo", "Ana", "Juan Perez", "X1", "Sofia", "Pedro", "123", "Renata", "Lu"]

    def rand_step(sim):
        accts = sorted(sim.acc)
        ops = sorted(sim.ops)
        choices = ["alta", "consulta", "deposito", "transfer", "transfer_big", "anula", "ping", "bad", "deposito_bad",
                   "consulta_missing", "unknown"]
        w = [3, 4, 4, 5, 2, 2, 1, 2, 1, 1, 1]
        kind = rng.choices(choices, w)[0]
        if kind == "alta":
            return M(f"ALTA;{rng.choice(names)}")
        if kind == "ping":
            return ("m", rng.choice(["PING;64", "ping;64", "PING;XX"]))
        if kind == "consulta" and accts:
            return M(f"CONSULTA;{rng.choice(accts)}")
        if kind == "consulta_missing":
            return M(f"CONSULTA;AC-{rng.randint(len(accts) + 1, 20):04d}")
        if kind == "deposito" and accts:
            return M(f"DEPOSITO;{rng.choice(accts)};{rng.choice([1, 100, 5000, 60000, 100000, 250000, 1000000, 9999999999])}")
        if kind == "deposito_bad" and accts:
            return M(f"DEPOSITO;{rng.choice(accts)};{rng.choice(['0', '-1', '01', '1.0', 'x', '10000000000'])}")
        if kind in ("transfer", "transfer_big") and len(accts) >= 2:
            o, d = rng.sample(accts, 2)
            amt = rng.choice([100001, 120000, 200000]) if kind == "transfer_big" else \
                rng.choice([1, 100, 500, 40000, 60000, 99999, 100000])
            return M(f"TRANSFER;{o};{d};{amt}")
        if kind == "anula":
            # keep to documented behaviour: do not anular ops whose origin has strikes (spec: supuesto)
            safe = [k for k in ops if sim.ops[k]["kind"] == "DEPOSITO" or sim.strikes.get(sim.ops[k]["o"], 0) == 0]
            if safe and rng.random() < 0.8:
                return M(f"ANULA;{rng.choice(safe)}")
            return M(f"ANULA;OP-{rng.randint(len(ops) + 1, 30):04d}")
        if kind == "bad":
            return ("m", rng.choice(["ALTA;Lucia;83", "CONSULTA;AC-0001;XX", "ALTA;Lucia;Gomez;11", "PING;64;64",
                                     "DEPOSITO;AC-0001;4", "ALTA;;85", "CONSULTA;AC-0001;00"]))
        if kind == "unknown":
            return M(rng.choice(["SALDOS;AC-0001", "HELP", "RETIRO;AC-0001;5", "CONSULTAR;AC-0001", "PONG", "INFO"]))
        return M(f"ALTA;{rng.choice(names)}")

    def gen(cid, kind, lo, hi):
        n = rng.randint(lo, hi)
        sim = Oracle(notas)
        steps = []
        for _ in range(n):
            s = rand_step(sim)
            sim.raw("POST", "/msg", J({"m": s[1]}))
            steps.append(s)
        b.case(cid, kind, steps)

    for i in range(15):
        gen(f"r-short-{i + 1:02d}", "short", 2, 5)
    for i in range(7):
        gen(f"r-long-{i + 1:02d}", "long", 6, 12)

    # ---------------------------------------------------------------- write
    kinds = {"single": 0, "short": 0, "long": 0}
    with open(OUT, "w", encoding="utf-8") as f:
        for c in b.cases:
            kinds[c["kind"]] += 1
            n = len(c["steps"])
            assert (c["kind"] == "single" and n == 1) or (c["kind"] == "short" and 2 <= n <= 5) or \
                   (c["kind"] == "long" and 6 <= n <= 12), (c["case_id"], n)
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    tot = sum(len(c["steps"]) for c in b.cases)
    der = sum(1 for c in b.cases for s in c["steps"] if s["source"] == "derived")
    print(f"cases={len(b.cases)} {kinds} steps={tot} observed={tot - der} derived={der} -> {OUT}")
    for c in b.cases:
        d = [i + 1 for i, s in enumerate(c["steps"]) if s["source"] == "derived"]
        if d:
            print(f"  derived steps in {c['case_id']}: {d}")


if __name__ == "__main__":
    main()

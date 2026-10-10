#!/usr/bin/env python3
"""Control-algorithm searcher: re-fits the control function offline from the log.

Oracle = experiments/log.jsonl only. From every POST /msg with m = "CMD;...;cc"
the pair (payload, control) is extracted:
  * ACCEPTED: response other than E100-E104/E201/E202/E301 and command != PING
    (PING is exempt, so it never reveals the control). Business errors such as
    E401/E506 count as accepted: E202 is checked before them.
  * REJECTED: E202.
  * "03"-style controls are excluded: they fail by canonical form (L1070), not by value.

Candidate family (cartesian product): case {upper,lower,raw} x kept separators
{none,';','-',';-'} x non-ASCII {drop,map} x alphabet {A=1..26, A=10..35, ASCII}
x aggregation {ISO 7064 concat + suffix, byte sum, weighted sum, polynomial}
x modulus {97,11,10,100,256,1000,10007} x formula {rem, 98-, 97-, m-, m+1-, Luhn,
Verhoeff, Damm}. Consistent candidates are ordered by simplicity.

Degraded mode: with no consistent candidate, control-fit.json gets
familia="permissive" (any 1-2 digit control is accepted, never E202). It loses
only the wrong-control cases expecting E202 and keeps every correct-control case.

Output control-fit.json keeps its field names and formula_texto wording frozen
(the replica's FitControl reads it).

USAGE
  python3 harness/fit.py                       # fit over the current log
  python3 harness/fit.py fit --from-jsonl x.jsonl
  python3 harness/fit.py plan                  # no fit: <=200-request probe plan -> fit-plan.jsonl
  python3 harness/fit.py ingest recorded.jsonl # re-fit with the plan's recorded responses
  python3 harness/fit.py --selftest
"""
from __future__ import annotations
import argparse
import json
import os
import time
from itertools import product

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOG = os.path.join(ROOT, "experiments", "log.jsonl")
FIT_OUT = os.path.join(ROOT, "harness", "control-fit.json")
PLAN_OUT = os.path.join(ROOT, "harness", "fit-plan.jsonl")

# Responses that say nothing about the control value.
EXCLUDE_CODES = {"E100", "E101", "E102", "E103", "E104", "E201", "E202", "E301", "E001"}
CONTROL_BAD_CODE = "E202"
EXEMPT_COMMANDS = {"PING"}

# --- Control digits: Luhn / Damm / Verhoeff (operate over a digit string)
def luhn_digit(s: str) -> int:
    total = 0
    for i, ch in enumerate(reversed(s)):
        d = int(ch)
        if i % 2 == 0:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return (10 - (total % 10)) % 10


_DAMM = [
    [0, 3, 1, 7, 5, 9, 8, 6, 4, 2],
    [7, 0, 9, 2, 1, 5, 4, 8, 6, 3],
    [4, 2, 0, 6, 8, 7, 1, 3, 5, 9],
    [1, 7, 5, 0, 9, 8, 3, 4, 2, 6],
    [6, 1, 2, 3, 0, 4, 5, 9, 7, 8],
    [3, 6, 7, 4, 2, 0, 9, 5, 8, 1],
    [5, 8, 6, 9, 7, 2, 0, 1, 3, 4],
    [8, 9, 4, 5, 3, 6, 2, 0, 1, 7],
    [9, 4, 3, 8, 6, 1, 7, 2, 0, 5],
    [2, 5, 8, 1, 4, 3, 6, 7, 9, 0],
]


def damm_digit(s: str) -> int:
    interim = 0
    for ch in s:
        interim = _DAMM[interim][int(ch)]
    return interim


_V_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_V_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]
_V_INV = [0, 4, 3, 2, 1, 5, 6, 7, 8, 9]


def verhoeff_digit(s: str) -> int:
    c = 0
    for i, ch in enumerate(reversed(s)):
        c = _V_D[c][_V_P[(i + 1) % 8][int(ch)]]
    return _V_INV[c]


DIGIT_FORMULAS = {"luhn": luhn_digit, "damm": damm_digit, "verhoeff": verhoeff_digit}

# --- Family parameters
CASES = ("upper", "lower", "raw")
KEEPS = ("", ";", "-", ";-")          # separators kept besides alphanumerics
NONASCII = ("drop", "map")
ALPHABETS = ("a1", "a10", "ascii")    # only relevant for iso_concat
AGGS = ("iso_concat", "sum_bytes", "sum_weighted", "poly")
SUFFIXES = ("00", "0", "")            # iso_concat only
MODULI = (97, 11, 10, 100, 256, 1000, 10007)
NUM_FORMULAS = ("rem", "98-", "97-", "m-", "m+1-")
DIGIT_FORMULA_NAMES = ("luhn", "verhoeff", "damm")
DEFAULT_POLY_BASES = (2, 7, 10, 16, 31, 33, 37, 41, 101, 256)


# --- Normalization and aggregation
def normalize(payload: str, case: str, keep: str, nonascii: str) -> str:
    if case == "upper":
        payload = payload.upper()
    elif case == "lower":
        payload = payload.lower()
    out = []
    for ch in payload:
        if ch.isascii():
            if ch.isalnum():
                out.append(ch)
            elif ch in keep:
                out.append(ch)
        else:
            if nonascii == "map":
                out.append(ch)
    return "".join(out)


def iso_digits(norm_s: str, alphabet: str) -> str:
    acc = []
    for ch in norm_s:
        if ch.isdigit() and ch.isascii():
            acc.append(ch)
        elif ch.isalpha():
            up = ch.upper()
            o = ord(up)
            if alphabet == "a1":
                if 65 <= o <= 90:
                    acc.append(str(o - 64))       # A=1..26
                else:
                    acc.append(str(ord(ch)))      # non A-Z letter (mapped accent)
            elif alphabet == "a10":
                if 65 <= o <= 90:
                    acc.append(str(o - 55))       # A=10..35
                else:
                    acc.append(str(ord(ch)))
            else:  # ascii ordinal
                acc.append(str(ord(ch)))
        else:
            # kept separator or mapped non-ASCII -> its ordinal
            acc.append(str(ord(ch)))
    return "".join(acc)


def apply_num_formula(n: int, m: int, formula: str) -> int:
    r = n % m
    if formula == "rem":
        return r
    if formula == "98-":
        return 98 - r
    if formula == "97-":
        return 97 - r
    if formula == "m-":
        return (m - r) % m
    if formula == "m+1-":
        return m + 1 - r
    raise ValueError(formula)


def formula_text(p: dict) -> str:
    # Written verbatim as control-fit.json formula_texto; wording is frozen.
    agg = p["agg"]
    keepdesc = {"": "descartar no-alfanuméricos", ";": "conservar ';'",
                "-": "conservar '-'", ";-": "conservar ';' y '-'"}[p["keep"]]
    norm = f"case={p['case']}, {keepdesc}, no-ASCII={p['nonascii']}"
    if agg == "iso_concat":
        alf = {"a1": "A=1..26", "a10": "A=10..35", "ascii": "ordinal ASCII"}[p["alphabet"]]
        body = f"concatenar dígitos ({alf}) + sufijo '{p['suffix']}'"
    elif agg == "sum_bytes":
        body = "N = suma de bytes"
    elif agg == "sum_weighted":
        body = "N = suma ponderada sum((i+1)*byte)"
    else:
        body = f"N = polinomio base {p['base']} (Horner)"
    f = p["formula"]
    if f in DIGIT_FORMULA_NAMES:
        tail = f"control = dígito {f.capitalize()} sobre la cadena de dígitos"
        return f"norm[{norm}]; {body}; {tail}"
    ftext = {"rem": "N % m", "98-": "98 - (N % m)", "97-": "97 - (N % m)",
             "m-": "(m - N%m) %% m", "m+1-": "m+1 - (N%m)"}[f]
    return f"norm[{norm}]; {body}; m={p['m']}; control = {ftext}"


# --- Evaluating a candidate
def make_control(p: dict):
    """Return a control(payload)->int|None function for params p."""
    case, keep, na = p["case"], p["keep"], p["nonascii"]
    agg = p["agg"]
    formula = p["formula"]

    def control(payload: str):
        s = normalize(payload, case, keep, na)
        if agg == "iso_concat":
            digits = iso_digits(s, p["alphabet"])
            if not digits:
                return None
            if formula in DIGIT_FORMULA_NAMES:
                return DIGIT_FORMULAS[formula](digits)
            n = int(digits + p["suffix"])
            return apply_num_formula(n, p["m"], formula)
        b = s.encode("utf-8", "surrogatepass") if any(ord(c) > 127 for c in s) else s.encode()
        if not b:
            return None
        if agg == "sum_bytes":
            n = sum(b)
        elif agg == "sum_weighted":
            n = sum((i + 1) * c for i, c in enumerate(b))
        else:  # poly, Horner base B
            base = p["base"]
            n = 0
            for c in b:
                n = n * base + c
        if formula in DIGIT_FORMULA_NAMES:
            return DIGIT_FORMULAS[formula](str(n))
        return apply_num_formula(n, p["m"], formula)

    return control


def simplicity(p: dict) -> int:
    cost = 0
    cost += {"iso_concat": 0, "sum_bytes": 2, "sum_weighted": 3, "poly": 4}[p["agg"]]
    cost += {"98-": 0, "rem": 1, "m-": 1, "97-": 2, "m+1-": 2,
             "luhn": 3, "damm": 4, "verhoeff": 4}[p["formula"]]
    cost += {97: 0, 10: 1, 11: 1, 100: 1, 256: 2, 1000: 2, 10007: 2}.get(p.get("m", 97), 2)
    cost += {"a1": 0, "a10": 1, "ascii": 2, None: 0}[p.get("alphabet")]
    cost += {"": 0, ";": 1, "-": 1, ";-": 2}[p["keep"]]
    cost += {"drop": 0, "map": 1}[p["nonascii"]]
    cost += {"00": 0, "0": 1, "": 1, None: 0}[p.get("suffix")]
    cost += {"upper": 0, "lower": 1, "raw": 1, None: 0}[p.get("case")]
    return cost


# --- Family generation (canonicalizes inactive dimensions to deduplicate)
def enumerate_family(poly_bases=DEFAULT_POLY_BASES):
    seen = set()
    raw = 0
    for case, keep, na in product(CASES, KEEPS, NONASCII):
        for agg in AGGS:
            if agg == "iso_concat":
                alpha_opts = ALPHABETS
                suffix_opts = SUFFIXES
            else:
                alpha_opts = (None,)
                suffix_opts = (None,)
            base_opts = poly_bases if agg == "poly" else (None,)
            for alphabet, suffix, base in product(alpha_opts, suffix_opts, base_opts):
                formulas = list(NUM_FORMULAS)
                # Luhn/Verhoeff/Damm need a digit string -> iso_concat
                if agg == "iso_concat":
                    formulas += list(DIGIT_FORMULA_NAMES)
                for formula in formulas:
                    mods = MODULI if formula not in DIGIT_FORMULA_NAMES else (None,)
                    for m in mods:
                        raw += 1
                        # case is irrelevant when the alphabet maps uppercase
                        eff_case = case
                        if agg == "iso_concat" and alphabet in ("a1", "a10") and keep == "" and na == "drop":
                            eff_case = None  # does not affect the result -> canonical
                        p = {
                            "case": eff_case, "keep": keep, "nonascii": na,
                            "agg": agg, "alphabet": alphabet, "suffix": suffix,
                            "base": base, "formula": formula, "m": m,
                        }
                        key = tuple(sorted(p.items(), key=lambda kv: kv[0]))
                        if key in seen:
                            continue
                        seen.add(key)
                        yield p
    enumerate_family.last_raw = raw


def space_size(poly_bases=DEFAULT_POLY_BASES):
    fam = list(enumerate_family(poly_bases))
    return getattr(enumerate_family, "last_raw", len(fam)), len(fam)


# --- Oracle: pair extraction from the log
def _code_of(res: dict):
    body = res.get("body", "")
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            return j.get("error")
    except Exception:
        return "NONJSON"
    return None


def extract_from_log(path: str):
    """Return (accepted, rejected): lists of (payload, control:int)."""
    records = []
    known_cmds = set()
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            # explicit pair format
            if "payload" in r and "control" in r:
                records.append(("pair", r))
                continue
            req = r.get("req", {})
            if req.get("path") != "/msg":
                continue
            # status 0 = network error logged by cn.py (timeout/reset): no evidence about the control
            if r.get("res", {}).get("status") == 0:
                continue
            b = req.get("body")
            m = b.get("m") if isinstance(b, dict) else b
            if not isinstance(m, str) or ";" not in m:
                continue
            code = _code_of(r.get("res", {}))
            cmd = m.split(";", 1)[0].upper()
            if code != "E301":
                known_cmds.add(cmd)
            records.append(("log", (m, code, cmd)))

    accepted, rejected = {}, {}
    for kind, data in records:
        if kind == "pair":
            payload = data["payload"]
            try:
                ctl = int(data["control"])
            except (TypeError, ValueError):
                continue
            (accepted if data.get("accepted", True) else rejected)[(payload, ctl)] = True
            continue
        m, code, cmd = data
        payload, cc = m.rsplit(";", 1)
        if not (cc.isdigit() and 1 <= len(cc) <= 2):
            continue
        ctl = int(cc)
        if code == CONTROL_BAD_CODE:
            # A control with a leading zero ("03") is rejected by canonical form
            # (L1070), not by value: it tells us nothing about the algorithm, so
            # it is excluded from the oracle.
            if len(cc) == 2 and cc[0] == "0":
                continue
            rejected[(payload, ctl)] = True
        elif code not in EXCLUDE_CODES and cmd in known_cmds and cmd not in EXEMPT_COMMANDS:
            accepted[(payload, ctl)] = True
    return list(accepted.keys()), list(rejected.keys())


# --- FIT
def fit(accepted, rejected, poly_bases=DEFAULT_POLY_BASES):
    """Return the list of consistent (params, control_fn), ordered by simplicity."""
    consistent = []
    for p in enumerate_family(poly_bases):
        ctl = make_control(p)
        ok = True
        for payload, c in accepted:
            try:
                if ctl(payload) != c:
                    ok = False
                    break
            except Exception:
                ok = False
                break
        if not ok:
            continue
        for payload, c in rejected:
            try:
                if ctl(payload) == c:  # a candidate must NOT validate an E202
                    ok = False
                    break
            except Exception:
                pass
        if ok:
            consistent.append(p)
    consistent.sort(key=simplicity)
    return consistent


def serialize(p: dict, n_verified: int) -> dict:
    familia = {
        "iso_concat": "iso7064" if p["formula"] in ("98-", "rem", "97-", "m-", "m+1-") else p["formula"],
        "sum_bytes": "bytesum", "sum_weighted": "weighted", "poly": "polynomial",
    }[p["agg"]]
    params = {k: v for k, v in p.items() if v is not None}
    return {"familia": familia, "params": params, "verificado_sobre": n_verified,
            "formula_texto": formula_text(p)}


def control_permissive(payload: str):
    """Degraded mode: None = do not verify the value (format check only, never E202)."""
    return None


def run_fit(accepted, rejected, poly_bases=DEFAULT_POLY_BASES, out=FIT_OUT, quiet=False):
    consistent = fit(accepted, rejected, poly_bases)
    n = len(accepted) + len(rejected)
    if not consistent:
        if not quiet:
            print("NO candidate is consistent with the oracle.")
            print("-> run 'plan' mode to discriminate with the original, or use degraded mode.")
            print("   (control-fit.json is written with familia='permissive')")
        spec = {"familia": "permissive", "params": {}, "verificado_sobre": n,
                "formula_texto": "modo degradado: aceptar cualquier control 1-2 dígitos (SUPUESTO)"}
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(spec, fh, ensure_ascii=False, indent=2)
        return None, consistent
    best = consistent[0]
    spec = serialize(best, n)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False, indent=2)
    if not quiet:
        print(f"Oracle: {len(accepted)} accepted + {len(rejected)} rejected (E202).")
        print(f"Consistent candidates: {len(consistent)}  (ordered by simplicity)")
        print()
        print(f"BEST  [familia={spec['familia']}, simplicity={simplicity(best)}]")
        print(f"  {spec['formula_texto']}")
        for i, p in enumerate(consistent[1:4], start=2):
            print(f"  #{i}: {formula_text(p)}")
        print()
        print(f"Written: {out}")
    return best, consistent


# --- PLAN
def run_plan(accepted, rejected, poly_bases=DEFAULT_POLY_BASES, out=PLAN_OUT,
             payloads=("ALTA;A", "ALTA;AB"), quiet=False):
    """Propose probes to the original to discriminate with <=200 requests."""
    # No survivor: enumerate the whole family's predictions.
    survivors = fit(accepted, rejected, poly_bases)
    if not survivors:
        survivors = list(enumerate_family(poly_bases))
    plan = []
    messages = []
    total_expected = 0
    total_max = 0
    for payload in payloads:
        preds = []
        seen = set()
        for p in survivors:
            try:
                c = make_control(p)(payload)
            except Exception:
                c = None
            if c is None or not (0 <= c <= 99):
                continue
            if c not in seen:
                seen.add(c)
                preds.append(c)
        order = preds + [c for c in range(100) if c not in seen]
        order = order[:100]
        # if some candidate is right the answer is among preds: expected cost ~ |preds|/2
        exp = (len(preds) + 1) // 2 if preds else 50
        total_expected += exp
        total_max += len(order)
        plan.append({"payload": payload, "controles_predichos": preds,
                     "orden_a_probar": order, "esperado": exp, "maximo": len(order)})
        for cc in order:
            messages.append(f"{payload};{cc}")
    with open(out, "w", encoding="utf-8") as fh:
        for msg in messages:
            fh.write(json.dumps({"m": msg}, ensure_ascii=False) + "\n")
    if not quiet:
        print("DISCRIMINATION PLAN (oracle = known command: 2xx/4xx != E202 = control OK):")
        for pl in plan:
            print(f"  payload {pl['payload']!r}: {len(pl['controles_predichos'])} distinct "
                  f"predictions, expected~{pl['esperado']} req, max {pl['maximo']} req")
        print(f"  TOTAL expected ~{total_expected} req, maximum {total_max} req "
              f"({'OK <=200' if total_max <= 200 else 'EXCEEDS 200'})")
        print(f"  Messages ready at: {out}")
        if total_max > 200:
            print("  WARNING: the maximum exceeds 200; trim payloads or use degraded mode.")
    return plan, total_expected, total_max


# --- INGEST
def run_ingest(extra_file: str, base_log=DEFAULT_LOG, poly_bases=DEFAULT_POLY_BASES,
               out=FIT_OUT, quiet=False):
    a1, r1 = extract_from_log(base_log) if os.path.exists(base_log) else ([], [])
    a2, r2 = extract_from_log(extra_file)
    accepted = list({**{k: 1 for k in a1}, **{k: 1 for k in a2}}.keys())
    rejected = list({**{k: 1 for k in r1}, **{k: 1 for k in r2}}.keys())
    if not quiet:
        print(f"Ingest: {len(a2)} accepted + {len(r2)} rejected new from {extra_file}")
    return run_fit(accepted, rejected, poly_bases, out, quiet)


# --- SELFTEST
def _gen_pairs(p, payloads):
    ctl = make_control(p)
    return [(pl, ctl(pl)) for pl in payloads if ctl(pl) is not None]


def selftest():
    t0 = time.time()
    raw, eff = space_size()
    print(f"Search space: {raw} raw combinations -> {eff} candidates (deduplicated).")

    # (1) Real log -> canonical ISO 7064 MOD 97-10
    accepted, rejected = extract_from_log(DEFAULT_LOG)
    assert accepted, "no accepted pairs extracted from the log"
    assert rejected, "no rejected E202 pairs extracted from the log"
    best, consistent = run_fit(accepted, rejected, quiet=True)
    assert best is not None, "no candidate found for the real log"
    assert best["agg"] == "iso_concat", f"agg={best['agg']}"
    assert best["alphabet"] == "a1", f"alphabet={best['alphabet']}"
    assert best["keep"] == "", f"keep={best['keep']!r}"
    assert best["nonascii"] == "drop", f"nonascii={best['nonascii']}"
    assert best["m"] == 97, f"m={best['m']}"
    assert best["formula"] == "98-", f"formula={best['formula']}"
    assert best["suffix"] == "00", f"suffix={best['suffix']}"
    assert best.get("case") is None, f"case={best.get('case')}"
    assert simplicity(consistent[0]) < simplicity(consistent[1]), "the simplest one is not unique"
    ref = _ref_control
    for pl, _ in accepted:
        assert make_control(best)(pl) == ref(pl), f"differs from control.py on {pl!r}"
    print(f"(1) real log -> ISO 7064 MOD 97-10, drop non-alnum, A=1..26 case-insensitive. "
          f"OK ({len(accepted)} accepted, {len(rejected)} E202; {len(consistent)} consistent)")

    probes = ["ALTA;Lucia", "CONSULTA;AC-0001", "TRANSFER;AC-0001;AC-0002;40000",
              "DEPOSITO;AC-0002;700", "FOO;BAR;BAZ", "X;1", "HELLO;WORLD;99",
              "ANULA;OP-0003", "SALDOS;AC-0009", "TEST;ABCDEF;123456"]

    # (2) Synthetic oracle: Luhn over digits (A=1..26)
    luhn_p = {"case": None, "keep": "", "nonascii": "drop", "agg": "iso_concat",
              "alphabet": "a1", "suffix": "", "base": None, "formula": "luhn", "m": None}
    pairs = _gen_pairs(luhn_p, probes)
    cons = fit(pairs, [], DEFAULT_POLY_BASES)
    assert cons, "Luhn: no candidate"
    recov = make_control(cons[0])
    held = ["QUUX;ZZ", "MANY;9", "ALPHA;BETA;7", "WX;YZ;11"]
    assert all(recov(h) == make_control(luhn_p)(h) for h in held), "Luhn does not reproduce held-out"
    assert cons[0]["formula"] == "luhn", f"recovered formula={cons[0]['formula']}"
    print(f"(2) synthetic Luhn over digits -> recovered (formula={cons[0]['formula']}, "
          f"{len(cons)} consistent, reproduces held-out). OK")

    # (3) Synthetic oracle: byte sum mod 100
    sb_p = {"case": "upper", "keep": ";-", "nonascii": "drop", "agg": "sum_bytes",
            "alphabet": None, "suffix": None, "base": None, "formula": "rem", "m": 100}
    pairs = _gen_pairs(sb_p, probes)
    cons = fit(pairs, [], DEFAULT_POLY_BASES)
    assert cons, "sum_bytes: no candidate"
    recov = make_control(cons[0])
    assert all(recov(h) == make_control(sb_p)(h) for h in held), "sum_bytes does not reproduce held-out"
    assert cons[0]["agg"] == "sum_bytes" and cons[0]["m"] == 100, f"recovered {cons[0]}"
    print(f"(3) synthetic byte sum mod 100 -> recovered (agg={cons[0]['agg']}, m={cons[0]['m']}, "
          f"{len(cons)} consistent, reproduces held-out). OK")

    dt = time.time() - t0
    print(f"\nTotal selftest time: {dt:.2f}s  ({'OK <30s' if dt < 30 else 'SLOW >=30s'})")
    assert dt < 30, "selftest took >=30s"
    print("SELFTEST: ALL OK")


def _ref_control(payload: str) -> int:
    acc = []
    for ch in payload:
        if ch.isalpha() and ch.isascii():
            acc.append(str(ord(ch.upper()) - ord("A") + 1))
        elif ch.isdigit() and ch.isascii():
            acc.append(ch)
    n = int("".join(acc) + "00")
    return 98 - (n % 97)


# --- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Control-algorithm searcher (offline).")
    ap.add_argument("mode", nargs="?", default="fit", choices=["fit", "plan", "ingest"])
    ap.add_argument("ingest_file", nargs="?", help="file of recorded responses (ingest mode)")
    ap.add_argument("--from-jsonl", dest="from_jsonl", help="alternative pairs/log file")
    ap.add_argument("--log", default=DEFAULT_LOG, help="oracle log (default experiments/log.jsonl)")
    ap.add_argument("--out", default=FIT_OUT)
    ap.add_argument("--plan-out", default=PLAN_OUT)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        selftest()
        return

    if args.mode == "ingest":
        if not args.ingest_file and not args.from_jsonl:
            ap.error("ingest requires a file of recorded responses")
        run_ingest(args.ingest_file or args.from_jsonl, base_log=args.log, out=args.out)
        return

    src = args.from_jsonl or args.log
    if not os.path.exists(src):
        ap.error(f"oracle file does not exist: {src}")
    accepted, rejected = extract_from_log(src)

    if args.mode == "plan":
        run_plan(accepted, rejected, out=args.plan_out)
        return

    best, consistent = run_fit(accepted, rejected, out=args.out)
    if best is None:
        print("\n-> run: python3 harness/fit.py plan")


if __name__ == "__main__":
    main()

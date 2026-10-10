#!/usr/bin/env python3
"""Automated adaptation cycle for Challenge 2 "The black box".

When the original service changes a rule during the day, this script derives the
next policy from *observed behaviour* instead of guessing it. It never calls the
original: it talks only to LOCAL emulators (a replica jar started on localhost)
and to recorded corpus files.

Pipeline (one `run` subcommand, five phases):

  1. detect     Replay the base corpus (recorded against the previous version)
                against the changed target and collect every step-level diff
                (case, step, message, expected, observed). Reuses replay.py.
  2. classify   Label each diff as a numeric-limit change, a response-format
                change, a status change for the same error code, or unknown, and
                group diffs by error code / response key.
  3. localize   Recover the exact new values from the target with surgical
                probes on a clean state (reset between probes; 3 failed
                transfers block the origin, so probes reset):
                  - transfer limit  -> bisect the maximum accepted amount.
                  - block threshold -> count failures until E510.
                  - response format -> derive the body template from observed
                    bodies, keeping the base placeholders; a numeric value that
                    is now a 2-decimal string becomes ${saldoDecimal}; a new
                    numeric key that increments 1,2,3 across repeats becomes
                    ${intentos} (the only counter the replica exposes).
                  - status change   -> take the observed status.
                  - initial balance -> read a fresh account's balance.
                Every probe request/response is written to
                control-plane/local/adapt-probes.jsonl and cited as evidence.
                Unknown diffs are reported, never guessed.
  4. propose    Write the candidate policy = base policy + localized changes,
                each with `value`/`status`/`body`, `evidencia` (probe lines) and
                `nota`. Print a human summary (what changed, old -> new).
  5. verify     Start a local jar with POLICY=<candidate> on a free port, replay
                the corpus recorded from the target, and report equal/total.
                Exit 0 only at 100%; otherwise list the remaining diffs (those
                need code, e.g. a new mechanism).

CLI:
  python3 adapt.py run --target http://localhost:8201 --token T \\
      --base rules/policy-v1.json --out rules/policy-candidate.json \\
      [--corpus control-plane/corpus] \\
      [--corpus-target-dir control-plane/corpus-v2 | --corpus-out DIR] \\
      [--budget N] [--no-verify]

Local targets only (localhost / 127.0.0.1); any other host is refused.
"""
import argparse
import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)            # control-plane/
REPO = os.path.dirname(ROOT)            # repository root
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(ROOT, "experiments"))

import compare as C          # noqa: E402  evaluator equality rule
import replay as R           # noqa: E402  load_cases / HttpxTransport / run / err_of / cmd_of
import control               # noqa: E402  control-digit helper

LOCAL_DIR = os.path.join(ROOT, "local")
PROBE_LOG = os.path.join(LOCAL_DIR, "adapt-probes.jsonl")
JAR = os.path.join(REPO, "replica", "build", "libs", "app.jar")
DEFAULT_BASE = os.path.join(REPO, "rules", "policy-v1.json")
DEFAULT_CORPUS = os.path.join(ROOT, "corpus")
CORPUS_FILES = ["battery.jsonl", "singles.jsonl", "sequences-short.jsonl",
                "sequences-long.jsonl", "pending.jsonl"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def decimal(cents):
    """Render an integer amount as a 2-decimal string (unit = 1/100), matching
    the replica's `decimal()` (22 -> "0.22", 1000000 -> "10000.00", -100 -> "-1.00")."""
    a = -cents if cents < 0 else cents
    return ("-" if cents < 0 else "") + str(a // 100) + "." + str(a % 100).rjust(2, "0")


def flatten(obj, prefix=""):
    """Flatten a JSON value to {dotted-path: scalar} (a.b, a[0].c)."""
    out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}[{i}]"))
    else:
        out[prefix] = obj
    return out


def _parse(body):
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        return None


def is_success(status):
    return isinstance(status, int) and 200 <= status < 300


def amount_in(m):
    """Last integer segment of a /msg payload (the amount), or None."""
    if not isinstance(m, str):
        return None
    segs = m.split(";")
    if len(segs) >= 2:                 # drop the trailing control segment
        segs = segs[:-1]
    for seg in reversed(segs):
        if re.fullmatch(r"-?\d+", seg):
            return int(seg)
    return None


def require_local(target):
    host = urlparse(target).hostname or ""
    if host not in ("localhost", "127.0.0.1", "::1"):
        print(f"[adapt] REFUSED target {target!r}: only localhost emulators are allowed.",
              file=sys.stderr)
        sys.exit(3)


def read_team_token():
    """TEAM_TOKEN from .env (never printed)."""
    path = os.path.join(REPO, ".env")
    try:
        for line in open(path):
            if line.startswith("TEAM_TOKEN="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def corpus_files(directory):
    files = [os.path.join(directory, f) for f in CORPUS_FILES
             if os.path.exists(os.path.join(directory, f))]
    if not files:  # fall back to any *.jsonl
        import glob
        files = sorted(glob.glob(os.path.join(directory, "*.jsonl")))
    return files


# --------------------------------------------------------------------------- #
# Probe transcript (evidence for local targets)
# --------------------------------------------------------------------------- #
class ProbeLog:
    def __init__(self, path):
        self.path = path
        self.lines = []

    def add(self, section, method, p, body, status, resbody):
        n = len(self.lines) + 1
        self.lines.append({"n": n, "section": section,
                           "req": {"method": method, "path": p, "body": body},
                           "res": {"status": status, "body": resbody}})
        return n

    def flush(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            for rec in self.lines:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def cite(ns):
        return [f"adapt-probes:L{n}" for n in ns]


class ProbeClient:
    """httpx client against a local emulator; every call is logged for evidence."""
    def __init__(self, target, token, log):
        import httpx
        self.c = httpx.Client(base_url=target.rstrip("/"),
                              headers={"Authorization": f"Bearer {token}"}, timeout=15.0)
        self.log = log

    def reset(self, section):
        r = self.c.post("/__reset")
        self.log.add(section, "POST", "/__reset", None, r.status_code, r.text)
        return r.status_code, r.text

    def send(self, payload, section):
        m = control.message(payload)
        r = self.c.post("/msg", json={"m": m})
        n = self.log.add(section, "POST", "/msg", {"m": m}, r.status_code, r.text)
        return r.status_code, r.text, n

    def close(self):
        self.c.close()


# --------------------------------------------------------------------------- #
# Phase 1: detect
# --------------------------------------------------------------------------- #
def detect(files, target, token):
    cases = R.load_cases(files)
    t = R.HttpxTransport(target, token)
    diffs = []
    for c in cases:
        t.reset()
        for i, step in enumerate(c["steps"], 1):
            exp_status = step.get("status")
            exp_body = step.get("body")
            if exp_status is None:                     # setup step, not compared
                if "m" in step:
                    t.msg(step["m"])
                continue
            if "raw" in step:
                r = step["raw"]
                status, body = t.raw(r["method"], r["path"], r.get("body"))
            else:
                status, body = t.msg(step["m"])
            ok, _ = C.compare({"status": exp_status, "body": exp_body},
                              {"status": status, "body": body})
            if ok:
                continue
            diffs.append({
                "case": c.get("case_id"), "step": i, "m": step.get("m"),
                "cmd": R.cmd_of(step),
                "exp": {"status": exp_status, "body": exp_body},
                "obs": {"status": status, "body": body},
                "exp_err": R.err_of(exp_body), "obs_err": R.err_of(body),
            })
    return diffs


# --------------------------------------------------------------------------- #
# Phase 2: classify
# --------------------------------------------------------------------------- #
def response_key_of(base, body):
    """Match a success body to a base `responses` key by its top-level key set."""
    obj = _parse(body)
    if not isinstance(obj, dict):
        return None
    keys = frozenset(obj.keys())
    for key, tpl in base.get("responses", {}).items():
        tobj = _parse(_template_to_json(tpl["body"]))
        if isinstance(tobj, dict) and frozenset(tobj.keys()) == keys:
            return key
    return None


def classify(diffs, base):
    """Return (groups, unknown). groups: label -> {cls, key, kind, diffs}."""
    groups = {}
    unknown = []
    for d in diffs:
        es, os_ = d["exp"]["status"], d["obs"]["status"]
        ee, oe = d["exp_err"], d["obs_err"]
        exp_succ, obs_succ = is_success(es) and not ee, is_success(os_) and not oe
        cls = key = kind = None
        if exp_succ and oe:                                    # 200 -> error
            cls, key, kind = ("limit" if amount_in(d["m"]) is not None else "unknown"), oe, "error"
        elif ee and obs_succ:                                  # error -> 200
            cls, key, kind = ("limit" if amount_in(d["m"]) is not None else "unknown"), ee, "error"
        elif ee and oe and ee == oe:                           # same error code
            body_diff = not C.bodies_equal(d["exp"]["body"], d["obs"]["body"])
            if es != os_ and body_diff:
                cls = "status+format"
            elif es != os_:
                cls = "status"
            else:
                cls = "format"
            key, kind = ee, "error"
        elif exp_succ and obs_succ:                            # same family, body differs
            rk = response_key_of(base, d["exp"]["body"]) or d["cmd"]
            ef, of = flatten(_parse(d["exp"]["body"]) or {}), flatten(_parse(d["obs"]["body"]) or {})
            shape = set(ef) != set(of) or any(
                type(ef[p]) is not type(of[p]) for p in set(ef) & set(of))
            cls, key, kind = ("format" if shape else "value"), rk, "response"
        else:                                                  # error -> different error, etc.
            cls, key, kind = "unknown", f"{ee}->{oe}", "error"
        if cls == "unknown":
            unknown.append(d)
            continue
        g = groups.setdefault(key, {"key": key, "kind": kind, "classes": set(), "diffs": []})
        g["classes"].add(cls)
        g["diffs"].append(d)
    return groups, unknown


# --------------------------------------------------------------------------- #
# Template derivation
# --------------------------------------------------------------------------- #
def _template_to_json(body):
    """Replace ${...} placeholders with parseable sentinel strings."""
    s = re.sub(r'"\$\{[^}]+\}"', '"\u0000Q\u0000"', body)
    s = re.sub(r'\$\{[^}]+\}', '"\u0000B\u0000"', s)
    return s


def _tpl_norm(body):
    """Normalize a body template to a comparable value: every ${name} (quoted or
    bare) becomes the string token <<name>> so placeholder NAMES are preserved
    (${saldo} != ${saldoDecimal}). Returns a parsed value, or the raw text."""
    s = re.sub(r'"\$\{([^}]+)\}"', lambda m: json.dumps(f"<<{m.group(1)}>>"), body)
    s = re.sub(r'\$\{([^}]+)\}', lambda m: json.dumps(f"<<{m.group(1)}>>"), s)
    return _parse(s)


def tpl_equal(a, b):
    """Template-aware equality: semantic JSON equality with placeholder names
    preserved; falls back to exact text when either is not JSON."""
    na, nb = _tpl_norm(a), _tpl_norm(b)
    if na is not None and nb is not None:
        return C._semantic_equal(na, nb)
    return a == b


def _base_placeholders(body):
    """Return (all_paths:set, placeholders:{path:(name,quoted)}, consts:{path:value})."""
    names = {}
    counter = [0]

    def repl_q(m):
        counter[0] += 1
        tok = f"\u0000Q{counter[0]}\u0000"
        names[tok] = (m.group(1), True)
        return json.dumps(tok)

    def repl_b(m):
        counter[0] += 1
        tok = f"\u0000B{counter[0]}\u0000"
        names[tok] = (m.group(1), False)
        return json.dumps(tok)

    s = re.sub(r'"\$\{([^}]+)\}"', repl_q, body)
    s = re.sub(r'\$\{([^}]+)\}', repl_b, s)
    obj = json.loads(s)
    fl = flatten(obj)
    ph, const = {}, {}
    for path, val in fl.items():
        if isinstance(val, str) and val in names:
            ph[path] = names[val]
        else:
            const[path] = val
    return set(fl.keys()), ph, const


def _is_counter(vals):
    """True if values are positive ints forming a consecutive run starting at 1."""
    if not vals or any(isinstance(v, bool) or not isinstance(v, int) for v in vals):
        return False
    ss = sorted(set(vals))
    return len(ss) >= 2 and ss[0] == 1 and all(ss[i + 1] - ss[i] == 1 for i in range(len(ss) - 1))


def derive_body(base_body, observed_list, expected_list=None):
    """Derive a new body template from observed bodies, keeping base placeholders.

    Returns (new_body, unknown_paths). observed_list/expected_list are parsed
    dict bodies (same order); expected_list may be None.
    """
    all_paths, ph, const = _base_placeholders(base_body)
    obs_flats = [flatten(o) for o in observed_list]
    exp_flats = [flatten(e) for e in (expected_list or []) if e is not None]
    rep = observed_list[-1]
    unknown = []

    def vals_at(path):
        return [fl[path] for fl in obs_flats if path in fl]

    def decide(path, value):
        if path in ph:
            name, quoted = ph[path]
            vals = vals_at(path)
            # numeric value now a 2-decimal string -> ${saldoDecimal}
            if (not quoted) and name == "saldo" and vals and all(isinstance(v, str) for v in vals):
                ok = True
                if exp_flats:
                    for idx, v in enumerate(vals):
                        ev = exp_flats[idx].get(path) if idx < len(exp_flats) else None
                        if not (isinstance(ev, int) and not isinstance(ev, bool) and decimal(ev) == v):
                            ok = False
                            break
                else:
                    ok = all(re.fullmatch(r"-?\d+\.\d{2}", v) for v in vals)
                if ok:
                    return '"${saldoDecimal}"'
            return (f'"${{{name}}}"' if quoted else f"${{{name}}}")
        if path not in all_paths:                              # new key
            if _is_counter(vals_at(path)):
                return "${intentos}"
            unknown.append(path)
            return json.dumps(value, ensure_ascii=False)
        return json.dumps(value, ensure_ascii=False)           # constant (observed literal)

    def build(obj, path):
        if isinstance(obj, dict):
            return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" +
                                  build(v, f"{path}.{k}" if path else k)
                                  for k, v in obj.items()) + "}"
        if isinstance(obj, list):
            return "[" + ",".join(build(v, f"{path}[{i}]") for i, v in enumerate(obj)) + "]"
        return decide(path, obj)

    return build(rep, ""), unknown


# --------------------------------------------------------------------------- #
# Phase 3: localize (probes against the target)
# --------------------------------------------------------------------------- #
def _setup_transfer(probe, section):
    probe.reset(section)
    probe.send("ALTA;L", section)                     # AC-0001
    probe.send("ALTA;M", section)                     # AC-0002 (distinct owner: no exemption)
    probe.send("DEPOSITO;AC-0001;9999999999", section)


def bisect_transfer_max(probe, base_tmax):
    """Bisect the maximum TRANSFER amount the target accepts. Resets each probe
    (a success/failure mutates strikes and could block the origin)."""
    sec = "localize:transferMax"

    def over(amount):
        _setup_transfer(probe, sec)
        s, b, n = probe.send(f"TRANSFER;AC-0001;AC-0002;{amount}", sec)
        return (R.err_of(b) == "E506"), n

    ladder = [1, 1000, 10000, 50000, 80000, 100000, 100001, 150000, 200000, 500000, 1000000]
    results = [(a, *over(a)) for a in ladder]
    accepted = [(a, n) for a, rej, n in results if not rej]
    rejected = [(a, n) for a, rej, n in results if rej]
    if not rejected:                                   # no cap found in range
        return accepted[-1][0], [n for _, _, n in results[-2:]]
    lo, lo_n = max(accepted, key=lambda x: x[0]) if accepted else (0, None)
    hi, hi_n = min(rejected, key=lambda x: x[0])
    ev = [n for n in (lo_n, hi_n) if n is not None]
    while hi - lo > 1:
        mid = (lo + hi) // 2
        rej, n = over(mid)
        if rej:
            hi, hi_n = mid, n
        else:
            lo, lo_n = mid, n
    ev = [n for n in (lo_n, hi_n) if n is not None]
    return lo, ev


def probe_block_e510(probe, over_amount):
    """Count failures until the origin blocks (E510), then collect a few E510
    responses so the attempt counter can be read. over_amount must exceed the
    transfer limit so each transfer is an E506 strike."""
    sec = "localize:block/E510"
    _setup_transfer(probe, sec)
    e506 = 0
    threshold = None
    e510 = []
    for _ in range(12):
        s, b, n = probe.send(f"TRANSFER;AC-0001;AC-0002;{over_amount}", sec)
        e = R.err_of(b)
        if e == "E506":
            e506 += 1
        elif e == "E510":
            if threshold is None:
                threshold = e506
            e510.append({"status": s, "parsed": _parse(b), "n": n})
            if len(e510) >= 3:
                break
        else:
            break
    return threshold, e510


def probe_balance(probe):
    """Read a fresh account's balance and the body shape of CONSULTA."""
    sec = "localize:balance/CONSULTA"
    probe.reset(sec)
    _, _, n0 = probe.send("ALTA;Bal", sec)                 # AC-0001
    s1, b1, n1 = probe.send("CONSULTA;AC-0001", sec)       # fresh balance
    dep = 123456
    probe.send(f"DEPOSITO;AC-0001;{dep}", sec)
    s3, b3, n3 = probe.send("CONSULTA;AC-0001", sec)       # after deposit
    j1, j3 = _parse(b1), _parse(b3)
    saldo = j1.get("saldo") if isinstance(j1, dict) else None
    if isinstance(saldo, str) and re.fullmatch(r"-?\d+\.\d{2}", saldo):
        init_cents, fmt = round(float(saldo) * 100), "decimal"
    elif isinstance(saldo, int) and not isinstance(saldo, bool):
        init_cents, fmt = saldo, "int"
    else:
        init_cents, fmt = None, "unknown"
    expected = None
    if init_cents is not None:
        expected = [{"cuenta_id": "AC-0001", "saldo": init_cents},
                    {"cuenta_id": "AC-0001", "saldo": init_cents + dep}]
    return {"init_cents": init_cents, "fmt": fmt, "observed": [j1, j3],
            "expected": expected, "ns": [n1, n3]}


def localize(diffs, groups, base, probe):
    """Run probes and return a list of change dicts."""
    changes = []
    base_tmax = base["limits"]["transferMax"]["value"]
    base_block = base["limits"]["blockAfterFailures"]["value"]
    base_init = base["limits"]["initialBalance"]["value"]

    has_transfer = any(d["cmd"] == "TRANSFER" for d in diffs)
    has_e510 = any("E510" in (d["exp_err"], d["obs_err"]) for d in diffs)
    consulta_diffs = [d for d in diffs if d["cmd"] == "CONSULTA"
                      and is_success(d["exp"]["status"]) and is_success(d["obs"]["status"])]
    # error codes (other than E510) that kept their code but changed status/body
    err_sf = {}
    for key, g in groups.items():
        if g["kind"] == "error" and key != "E510" and ("status" in g["classes"]
                                                        or "format" in g["classes"]
                                                        or "status+format" in g["classes"]):
            err_sf[key] = g["diffs"]

    measured_tmax = None
    if has_transfer:
        measured_tmax, ev = bisect_transfer_max(probe, base_tmax)
        if measured_tmax != base_tmax:
            changes.append({"path": ("limits", "transferMax"), "cls": "numeric-limit",
                            "updates": {"value": measured_tmax},
                            "old": base_tmax, "new": measured_tmax,
                            "evidencia": ProbeLog.cite(ev),
                            "nota": f"v-next: per-operation transfer limit {base_tmax} -> "
                                    f"{measured_tmax} (inclusive; bisection against the target)."})

    if has_e510:
        over_amt = (measured_tmax if measured_tmax is not None else base_tmax) + 1
        threshold, e510 = probe_block_e510(probe, over_amt)
        if threshold is not None and threshold != base_block:
            changes.append({"path": ("limits", "blockAfterFailures"), "cls": "numeric-limit",
                            "updates": {"value": threshold},
                            "old": base_block, "new": threshold,
                            "evidencia": ProbeLog.cite([o["n"] for o in e510]),
                            "nota": f"v-next: origin blocks after {threshold} failures "
                                    f"(was {base_block}); counted failures until E510."})
        if e510:
            base_e510 = base["errors"]["E510"]
            obs_status = e510[0]["status"]
            new_body, unk = derive_body(base_e510["body"], [o["parsed"] for o in e510])
            upd = {}
            if obs_status != base_e510["status"]:
                upd["status"] = obs_status
            if not tpl_equal(new_body, base_e510["body"]):
                upd["body"] = new_body
            if upd:
                changes.append({"path": ("errors", "E510"),
                                "cls": "status/format", "updates": upd,
                                "old": f'{base_e510["status"]} {base_e510["body"]}',
                                "new": f'{upd.get("status", base_e510["status"])} '
                                       f'{upd.get("body", base_e510["body"])}',
                                "evidencia": ProbeLog.cite([o["n"] for o in e510]),
                                "nota": "v-next: E510 status/body changed "
                                        "(new attempt counter -> ${intentos})."})

    for code, dlist in err_sf.items():
        node = base["errors"].get(code)
        if not node:
            continue
        obs_status = dlist[0]["obs"]["status"]
        obs_parsed = [_parse(d["obs"]["body"]) for d in dlist]
        exp_parsed = [_parse(d["exp"]["body"]) for d in dlist]
        new_body, unk = derive_body(node["body"], [o for o in obs_parsed if o], exp_parsed)
        upd = {}
        if obs_status != node["status"]:
            upd["status"] = obs_status
        if not tpl_equal(new_body, node["body"]):
            upd["body"] = new_body
        if upd:
            changes.append({"path": ("errors", code), "cls": "status/format", "updates": upd,
                            "old": f'{node["status"]} {node["body"]}',
                            "new": f'{upd.get("status", node["status"])} '
                                   f'{upd.get("body", node["body"])}',
                            "evidencia": [f"corpus-diff:{d['case']}#{d['step']}" for d in dlist[:4]],
                            "nota": f"v-next: {code} status/body changed."})

    if consulta_diffs:
        res = probe_balance(probe)
        if res["init_cents"] is not None and res["init_cents"] != base_init:
            changes.append({"path": ("limits", "initialBalance"), "cls": "numeric-value",
                            "updates": {"value": res["init_cents"]},
                            "old": base_init, "new": res["init_cents"],
                            "evidencia": ProbeLog.cite(res["ns"]),
                            "nota": f"v-next: initial account balance {base_init} -> "
                                    f"{res['init_cents']} (read from a fresh account)."})
        base_cons = base["responses"]["CONSULTA"]
        new_body, unk = derive_body(base_cons["body"], res["observed"], res["expected"])
        if not tpl_equal(new_body, base_cons["body"]):
            changes.append({"path": ("responses", "CONSULTA"), "cls": "response-format",
                            "updates": {"body": new_body},
                            "old": base_cons["body"], "new": new_body,
                            "evidencia": ProbeLog.cite(res["ns"]),
                            "nota": "v-next: CONSULTA balance rendered as a 2-decimal "
                                    "string -> ${saldoDecimal}." if "saldoDecimal" in new_body
                            else "v-next: CONSULTA body shape changed."})
    return changes


# --------------------------------------------------------------------------- #
# Phase 4: propose
# --------------------------------------------------------------------------- #
def propose(base, changes):
    cand = copy.deepcopy(base)
    cand["version"] = "candidate"
    cand["descripcion"] = ("Candidato derivado automaticamente por adapt.py a partir del "
                           "comportamiento observado del servicio cambiado. "
                           "Base: " + base.get("version", "?") + ".")
    for ch in changes:
        node = cand
        for p in ch["path"][:-1]:
            node = node[p]
        target = node[ch["path"][-1]]
        for f, v in ch["updates"].items():
            target[f] = v
        target["evidencia"] = ch["evidencia"]
        target["nota"] = ch["nota"]
    return cand


def print_summary(changes, unknown):
    print("\n[adapt] === proposed changes ===")
    if not changes:
        print("   (none: the target matches the base policy, or every diff is unknown)")
    for ch in changes:
        path = ".".join(ch["path"])
        print(f"   * {path}  [{ch['cls']}]")
        print(f"       {ch['old']}  ->  {ch['new']}")
        print(f"       evidence: {', '.join(map(str, ch['evidencia']))}")
        print(f"       nota: {ch['nota']}")
    if unknown:
        print(f"\n[adapt] === {len(unknown)} UNKNOWN diff(s) reported (not guessed) ===")
        for d in unknown[:10]:
            print(f"   {d['case']} step {d['step']} [{d['cmd']}]: "
                  f"{d['exp']['status']} {d['exp_err'] or ''} -> "
                  f"{d['obs']['status']} {d['obs_err'] or ''}")
        if len(unknown) > 10:
            print(f"   ... and {len(unknown) - 10} more")


# --------------------------------------------------------------------------- #
# Phase 5: verify
# --------------------------------------------------------------------------- #
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch_jar(policy_path, port, token):
    env = dict(os.environ)
    env.update({"PORT": str(port), "TEAM_TOKEN": token, "HEALTH_INTERNAL": "1",
                "POLICY": policy_path})
    logf = open(os.path.join(LOCAL_DIR, f"adapt-verify-{port}.log"), "w")
    os.makedirs(LOCAL_DIR, exist_ok=True)
    proc = subprocess.Popen(["java", "-jar", JAR], env=env, stdout=logf, stderr=subprocess.STDOUT)
    return proc, logf


def wait_health(port, token, timeout=60):
    import httpx
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"http://localhost:{port}/health",
                          headers={"Authorization": f"Bearer {token}"}, timeout=2.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def verify(candidate_path, corpus_dir, token):
    files = corpus_files(corpus_dir)
    cases = R.load_cases(files)
    port = free_port()
    proc, logf = launch_jar(candidate_path, port, token)
    try:
        if not wait_health(port, token):
            print(f"[adapt] verify: jar on :{port} did not become healthy", file=sys.stderr)
            return None
        t = R.HttpxTransport(f"http://localhost:{port}", token)
        res = R.run(cases, t, max_diffs=40)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        logf.close()
    return res


# --------------------------------------------------------------------------- #
# record from target (optional, reuses record.py)
# --------------------------------------------------------------------------- #
def record_from_target(target, token, out_dir, budget):
    import record as REC
    REC.OG = REC.TargetTransport(target, token)
    REC.CORPUS = out_dir if os.path.isabs(out_dir) else os.path.join(ROOT, out_dir)
    REC.SELECT = None
    os.makedirs(REC.CORPUS, exist_ok=True)
    b = REC.Budget(budget)
    REC.do_battery(b)
    REC.do_singles(b)
    REC.do_sequences(b)
    REC.do_pending(b)
    return REC.CORPUS


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def cmd_run(args):
    require_local(args.target)
    token = args.token or read_team_token()
    if not token:
        print("[adapt] no token (pass --token or set TEAM_TOKEN in .env)", file=sys.stderr)
        sys.exit(2)
    base = json.load(open(args.base, encoding="utf-8"))

    print(f"[adapt] detect: replaying base corpus ({args.corpus}) against {args.target}")
    diffs = detect(corpus_files(args.corpus), args.target, token)
    print(f"[adapt] detect: {len(diffs)} diff step(s)")

    groups, unknown = classify(diffs, base)
    print("[adapt] classify: groups by error-code / response-key:")
    for key, g in sorted(groups.items()):
        print(f"   {key}: {sorted(g['classes'])}  ({len(g['diffs'])} diff(s), {g['kind']})")

    log = ProbeLog(PROBE_LOG)
    probe = ProbeClient(args.target, token, log)
    try:
        changes = localize(diffs, groups, base, probe)
    finally:
        probe.close()
        log.flush()
    print(f"[adapt] localize: {len(changes)} change(s); probe transcript -> "
          f"{os.path.relpath(PROBE_LOG, REPO)} ({len(log.lines)} lines)")

    cand = propose(base, changes)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cand, f, ensure_ascii=False, indent=2)
    print(f"[adapt] propose: candidate -> {args.out}")
    print_summary(changes, unknown)

    if args.no_verify:
        return 0

    corpus_dir = args.corpus_target_dir
    if not corpus_dir:
        corpus_dir = args.corpus_out or os.path.join(LOCAL_DIR, "adapt-corpus-target")
        print(f"[adapt] verify: recording corpus from target into {corpus_dir}")
        record_from_target(args.target, token, corpus_dir, args.budget)
    print(f"[adapt] verify: replaying {corpus_dir} against a jar with the candidate")
    res = verify(args.out, corpus_dir, token)
    if res is None:
        return 2
    pct = 100.0 * res["equal_steps"] / res["total_steps"] if res["total_steps"] else 0.0
    print(f"\n[adapt] verify: equal={res['equal_steps']}/{res['total_steps']} ({pct:.2f}%)  "
          f"cases 100%={res['case_pass']}/{res['n_cases']}")
    if res["equal_steps"] == res["total_steps"]:
        print("[adapt] VERIFIED at 100% — candidate reproduces the target.")
        return 0
    print("[adapt] remaining diffs (need code, e.g. a new mechanism):")
    for cid, i, cmd, reason in res["diffs"]:
        print(f"   {cid} step {i} [{cmd}]: {reason.splitlines()[0]}")
    return 1


def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="detect -> classify -> localize -> propose -> verify")
    r.add_argument("--target", required=True, help="URL of the changed LOCAL emulator")
    r.add_argument("--token", default=None, help="Bearer token (default: TEAM_TOKEN from .env)")
    r.add_argument("--base", default=DEFAULT_BASE)
    r.add_argument("--out", required=True)
    r.add_argument("--corpus", default=DEFAULT_CORPUS, help="base corpus directory")
    r.add_argument("--corpus-target-dir", default=None,
                   help="corpus already recorded from the target (for verify)")
    r.add_argument("--corpus-out", default=None,
                   help="record the target's corpus into this directory before verify")
    r.add_argument("--budget", type=int, default=3000)
    r.add_argument("--no-verify", action="store_true")
    r.set_defaults(func=cmd_run)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

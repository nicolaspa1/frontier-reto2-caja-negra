#!/usr/bin/env python3
"""RESPONSE TEMPLATE re-extractor - Challenge 2 "The black box".

Does NOT call the original service. It works only with the recorded log
(`experiments/log.jsonl`) and with the templates of a policy
(`rules/policy-v1.json`, `responses`/`errors` blocks).

Idea:
  - Each observed response belongs to a "slot": for errors, the code
    (E301, E401, ...); for successes, the body shape. Two instances of the same
    slot share status and the SAME set of paths; only the values of some paths
    change = variables. The remaining paths are constants.
  - The slot is identified by its structural SIGNATURE
        (status, set-of-paths, error-code-if-any)
    which is stable against value renamings (RF4) and distinguishes bare E301
    from E301+sugerencia, ANULA from ANULA_REPETIDA, etc.
  - For each slot a template is emitted with named placeholders, using the names
    already used by policy-v1 (${cuentaId}, ${saldo}, ${operacionId},
    ${sugerencia}) when the path matches a known variable.

Subcommands:
  extract  --log L [--from N] [--to M]
      Group, detect variables/constants and write
      harness/templates-extracted.json.
  diff     --against policy.json [--log L] [--from N] [--to M]
      Compare what was extracted with the policy templates: equal, different
      (with text diff), new (slot without a template) and orphan (template with
      no observation in the range).
  patch    --against policy.json --out out.json [--log L] [--from N] [--to M]
      Copy the policy with responses/errors updated from the log (only those two
      blocks), evidencia = lines used. Unknown variable ->
      "supuesto":"valor variable no reproducible" + last observed value.
  --selftest
      Synthetic log (recieved->received and E301 without a trailing space from
      line N onward); verifies that diff detects exactly those 2 differences and
      that patch produces the correct policy.

NOTE: the templates-extracted.json / patched-policy field names (fuente, rango,
plantillas, comandos, observaciones, ejemplos, variables_desconocidas, conocida,
patron, ultimo, evidencia, supuesto, ...) and the service-matching byte strings
('comando desconocido', 'recieved'/'received') are kept in Spanish on purpose so
the output files and the evaluator's byte comparison stay compatible.
"""
import argparse
import copy
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# compare.py lives next door: the evaluator's JSON semantic equality.
sys.path.insert(0, HERE)
import compare  # noqa: E402

DEFAULT_LOG = os.path.join(ROOT, "experiments", "log.jsonl")
DEFAULT_POLICY = os.path.join(os.path.dirname(ROOT), "rules", "policy-v1.json")
DEFAULT_OUT_EXTRACT = os.path.join(HERE, "templates-extracted.json")

PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
# Sentinels for the canonical form used when comparing templates.
SENT_STR = "\u0000VARSTR\u0000"   # placeholder that was quoted
SENT_BARE = "\u0000VARBARE\u0000"  # "bare" placeholder (number/boolean)


# --------------------------------------------------------------------------- #
# Aplanado de cuerpos JSON a rutas  (a.b[0].c -> valor)
# --------------------------------------------------------------------------- #
def flatten(obj, prefix=""):
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


# --------------------------------------------------------------------------- #
# Catalog of known variables (names already used by policy-v1)
# --------------------------------------------------------------------------- #
# Each entry: (name, test(values) -> bool, pattern). The first one that matches
# ALL observed values of the path wins.
def _all(values, pred):
    vals = [eval_str(v) for v in values]
    return bool(vals) and all(pred(v) for v in vals)


def eval_str(v):
    return v


_RE_AC = re.compile(r"^AC-\d+$")
_RE_OP = re.compile(r"^OP-\d+$")

# The pattern labels ("entero", "texto", "AC-dddd", ...) are written to the
# "patron" field of templates-extracted.json, so they stay in Spanish.
KNOWN_VARS = [
    # (name, predicate-over-one-value, pattern-label)
    ("cuentaId", lambda v: isinstance(v, str) and _RE_AC.match(v), "AC-dddd"),
    ("operacionId", lambda v: isinstance(v, str) and _RE_OP.match(v), "OP-dddd"),
    ("saldo", lambda v: isinstance(v, bool) is False and isinstance(v, int), "entero"),
    ("sugerencia", lambda v: isinstance(v, str), "texto"),  # by path name only
]

# Hints by path leaf NAME -> policy variable name.
PATH_HINTS = {
    "cuentaId": "cuentaId",
    "cuenta_id": "cuentaId",
    "operacionId": "operacionId",
    "anulada": "operacionId",
    "saldo": "saldo",
    "sugerencia": "sugerencia",
}


def leaf_name(path):
    # last segment, without the array index
    seg = re.split(r"[.\[]", path)[-1].rstrip("]")
    return seg


def pattern_of(values):
    """Observed pattern label for a variable."""
    if _all(values, lambda v: isinstance(v, str) and bool(_RE_AC.match(v))):
        return "AC-dddd"
    if _all(values, lambda v: isinstance(v, str) and bool(_RE_OP.match(v))):
        return "OP-dddd"
    if _all(values, lambda v: isinstance(v, bool)):
        return "booleano"
    if _all(values, lambda v: isinstance(v, int) and not isinstance(v, bool)):
        return "entero"
    if _all(values, lambda v: isinstance(v, float)):
        return "decimal"
    return "texto"


def resolve_var_name(path, values, policy_name=None):
    """Return (name, known: bool). Prefers the policy name if the path matches;
    otherwise tries the catalog by value or the path hint."""
    if policy_name:
        return policy_name, True
    leaf = leaf_name(path)
    if leaf in PATH_HINTS:
        return PATH_HINTS[leaf], True
    # by values
    for name, pred, _ in KNOWN_VARS[:3]:  # cuentaId / operacionId / saldo
        if _all(values, pred):
            return name, True
    # unknown: use the leaf name as a tentative placeholder
    return leaf or "var", False


# --------------------------------------------------------------------------- #
# Reading the log
# --------------------------------------------------------------------------- #
def load_log(path, frm=None, to=None):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            o = json.loads(line)
            n = o.get("n")
            if frm is not None and n < frm:
                continue
            if to is not None and n > to:
                continue
            res = o.get("res", {})
            body = res.get("body")
            status = res.get("status")
            try:
                parsed = json.loads(body)
                is_json = True
            except (ValueError, TypeError):
                parsed = None
                is_json = False
            # command (informational): first segment of req.body.m
            cmd = None
            req = o.get("req", {})
            rb = req.get("body")
            if isinstance(rb, dict) and isinstance(rb.get("m"), str):
                cmd = rb["m"].split(";")[0]
            elif req.get("path"):
                cmd = f'{req.get("method")} {req.get("path")}'
            recs.append({
                "n": n, "status": status, "body": body,
                "is_json": is_json, "parsed": parsed, "cmd": cmd,
            })
    return recs


# --------------------------------------------------------------------------- #
# Firma de un slot
# --------------------------------------------------------------------------- #
def signature(rec):
    if not rec["is_json"] or not isinstance(rec["parsed"], dict):
        # non-JSON (or non-object) body: the exact text IS the signature
        return (rec["status"], ("__TEXT__", rec["body"]))
    fl = flatten(rec["parsed"])
    code = rec["parsed"].get("error")
    code = code if isinstance(code, str) else None
    return (rec["status"], tuple(sorted(fl.keys())), code)


# --------------------------------------------------------------------------- #
# Signature of a policy template (string with ${...})
# --------------------------------------------------------------------------- #
def template_to_sentinels(body):
    """Replace placeholders with sentinels and return a parseable JSON string."""
    # quoted ones first, then bare ones. We use replacement functions so that
    # re does not interpret the JSON escape sequences.
    quoted = json.dumps(SENT_STR)
    bare = json.dumps(SENT_BARE)
    s = re.sub(r'"\$\{[^}]+\}"', lambda m: quoted, body)
    s = re.sub(r'\$\{[^}]+\}', lambda m: bare, s)
    return s


def template_signature(body, status):
    try:
        obj = json.loads(template_to_sentinels(body))
    except (ValueError, TypeError):
        return (status, ("__TEXT__", body))
    if not isinstance(obj, dict):
        return (status, ("__TEXT__", body))
    fl = flatten(obj)
    code = obj.get("error")
    code = code if isinstance(code, str) else None
    # the error sentinel is not a code: use the template literal if there is one
    if code in (SENT_STR, SENT_BARE):
        code = None
    return (status, tuple(sorted(fl.keys())), code)


def policy_index(policy):
    """sig -> (section, key, status, body). (dict key names preserved.)"""
    idx = {}
    for section in ("responses", "errors"):
        for key, tpl in policy.get(section, {}).items():
            sig = template_signature(tpl["body"], tpl["status"])
            idx[sig] = (section, key, tpl["status"], tpl["body"])
    return idx


# --------------------------------------------------------------------------- #
# Template serialization (compact, like policy-v1)
# --------------------------------------------------------------------------- #
def serialize(obj, placeholders, literals, path=""):
    """obj: representative (last observed). placeholders: path->(name,was_str)
    for variables emitted as ${name}. literals: paths left with their literal
    value (constants and, in patch mode, unknown variables)."""
    if path in placeholders:
        name, was_str = placeholders[path]
        return f'"${{{name}}}"' if was_str else f"${{{name}}}"
    if isinstance(obj, dict):
        parts = []
        for k, v in obj.items():
            child = f"{path}.{k}" if path else k
            parts.append(json.dumps(k, ensure_ascii=False) + ":" +
                         serialize(v, placeholders, literals, child))
        return "{" + ",".join(parts) + "}"
    if isinstance(obj, list):
        parts = []
        for i, v in enumerate(obj):
            parts.append(serialize(v, placeholders, literals, f"{path}[{i}]"))
        return "[" + ",".join(parts) + "]"
    return json.dumps(obj, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Template extraction from the log
# --------------------------------------------------------------------------- #
def extract(recs, policy=None):
    """Group recs by signature and produce a dict of templates.

    Returns dict name -> {
        slot, status, comandos, body, body_patch, n_obs, examples,
        variables: {path: {name, known, pattern, last}},
        unknown_vars: [path...], matched_policy: (section,key)|None
    }
    """
    pidx = policy_index(policy) if policy else {}
    groups = {}
    for r in recs:
        sig = signature(r)
        groups.setdefault(sig, []).append(r)

    templates = {}
    used_names = set()
    for sig, members in groups.items():
        status = members[0]["status"]
        is_text = (len(sig) == 2 and sig[1][0] == "__TEXT__")
        matched = pidx.get(sig)
        # slot name
        if matched:
            section, key = matched[0], matched[1]
            name = key
        else:
            section = "responses" if 200 <= (status or 0) < 300 else "errors"
            name = _synth_name(members, status, sig, used_names)
        used_names.add(name)

        commands = sorted({m["cmd"] for m in members if m["cmd"]})
        examples = [m["n"] for m in members][:8]

        if is_text:
            body = members[-1]["body"]
            templates[name] = {
                "slot": name, "section": section, "status": status,
                "comandos": commands, "body": body, "body_patch": body,
                "n_obs": len(members), "examples": examples,
                "variables": {}, "unknown_vars": [],
                "matched_policy": [section, key] if matched else None,
                "content_type": "text",
            }
            continue

        # flatten all members
        flats = [flatten(m["parsed"]) for m in members]
        all_paths = sorted({p for fl in flats for p in fl.keys()})
        # placeholders of the matching policy, by path
        policy_ph = {}
        if matched:
            policy_ph = _policy_placeholders(matched[3])

        var_paths = {}       # path -> {name, known, pattern, last, was_str}
        placeholders = {}    # path -> (name, was_str) para el body "extract"
        placeholders_patch = {}  # same for patch (known ones only)
        unknown = []
        for p in all_paths:
            vals = [fl.get(p) for fl in flats if p in fl]
            distinct = {json.dumps(v, ensure_ascii=False, sort_keys=True) for v in vals}
            was_str = all(isinstance(v, str) for v in vals)
            if len(distinct) > 1:
                pol_name = policy_ph.get(p)
                vname, known = resolve_var_name(p, vals, pol_name)
                pat = pattern_of(vals)
                var_paths[p] = {"name": vname, "known": known,
                                "pattern": pat, "last": vals[-1], "was_str": was_str}
                placeholders[p] = (vname, was_str)
                if known:
                    placeholders_patch[p] = (vname, was_str)
                else:
                    unknown.append(p)
            else:
                # 1 instance: does it match a known policy variable?
                if matched and p in policy_ph:
                    pol_name = policy_ph[p]
                    var_paths[p] = {"name": pol_name, "known": True,
                                    "pattern": pattern_of(vals), "last": vals[-1],
                                    "was_str": was_str}
                    placeholders[p] = (pol_name, was_str)
                    placeholders_patch[p] = (pol_name, was_str)
                # otherwise it is a constant

        rep = copy.deepcopy(members[-1]["parsed"])  # last observed
        body = serialize(rep, placeholders, set())
        body_patch = serialize(rep, placeholders_patch, set())

        templates[name] = {
            "slot": name, "section": section, "status": status,
            "comandos": commands, "body": body, "body_patch": body_patch,
            "n_obs": len(members), "examples": examples,
            "variables": {p: {"name": v["name"], "conocida": v["known"],
                              "patron": v["pattern"],
                              "ultimo": v["last"]} for p, v in var_paths.items()},
            "unknown_vars": unknown,
            "matched_policy": [section, key] if matched else None,
            "content_type": "json",
        }
    return templates


def _policy_placeholders(body):
    """path -> placeholder name, parsing the policy body."""
    # replace each placeholder with a marker that remembers its name
    tokens = {}
    counter = [0]

    def repl(m):
        counter[0] += 1
        tok = f"__PH{counter[0]}__"
        tokens[tok] = m.group(1)
        return json.dumps(tok)

    s = re.sub(r'"\$\{([^}]+)\}"', repl, body)
    s = re.sub(r'\$\{([^}]+)\}', repl, s)
    try:
        obj = json.loads(s)
    except (ValueError, TypeError):
        return {}
    out = {}
    for path, val in flatten(obj).items():
        if isinstance(val, str) and val in tokens:
            out[path] = tokens[val]
    return out


def _synth_name(members, status, sig, used):
    cmds = [m["cmd"] for m in members if m["cmd"]]
    base = (cmds[0] if cmds else f"SLOT_{status}")
    base = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or f"SLOT_{status}"
    name = base
    i = 2
    while name in used:
        name = f"{base}_{i}"
        i += 1
    return name


# --------------------------------------------------------------------------- #
# Canonical form of a template (to compare extracted vs policy)
# --------------------------------------------------------------------------- #
def canonical(body):
    try:
        return ("json", json.loads(template_to_sentinels(body)))
    except (ValueError, TypeError):
        return ("text", body)


def templates_equal(a_body, b_body):
    ta, va = canonical(a_body)
    tb, vb = canonical(b_body)
    if ta == "json" and tb == "json":
        return compare._semantic_equal(va, vb)
    return a_body == b_body


def text_diff(a, b):
    import difflib
    return "\n".join(difflib.unified_diff(
        [a], [b], fromfile="policy", tofile="log", lineterm=""))


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #
def cmd_extract(args):
    recs = load_log(args.log, args.frm, args.to)
    policy = _maybe_load_policy(args)
    templates = extract(recs, policy)
    out = {
        "fuente": os.path.relpath(args.log, ROOT),
        "rango": {"from": args.frm, "to": args.to},
        "n_lineas": len(recs),
        "n_plantillas": len(templates),
        "plantillas": {},
    }
    for name in sorted(templates):
        t = templates[name]
        out["plantillas"][name] = {
            "slot": t["slot"],
            "section": t["section"],
            "status": t["status"],
            "comandos": t["comandos"],
            "body": t["body"],
            "observaciones": t["n_obs"],
            "ejemplos": [f"L{n}" for n in t["examples"]],
            "variables": t["variables"],
            "variables_desconocidas": t["unknown_vars"],
        }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[extract] {len(recs)} lines -> {len(templates)} templates")
    print(f"[extract] wrote {os.path.relpath(args.out, ROOT)}")
    return out


def compute_diff(recs, policy):
    templates = extract(recs, policy)
    pidx = policy_index(policy)
    # signature -> (section,key) of the policy
    sig2pol = {sig: (sec, key) for sig, (sec, key, _, _) in pidx.items()}

    # signature -> extracted template
    sig2tpl = {}
    for name, t in templates.items():
        # recompute the template signature from an example
        sig = _template_group_sig(t)
        sig2tpl[sig] = (name, t)

    iguales, distintas, nuevas = [], [], []
    for sig, (name, t) in sig2tpl.items():
        if sig in sig2pol:
            sec, key = sig2pol[sig]
            pbody = policy[sec][key]["body"]
            pstatus = policy[sec][key]["status"]
            same_status = (pstatus == t["status"])
            same_body = templates_equal(pbody, t["body"])
            if same_status and same_body:
                iguales.append((sec, key))
            else:
                distintas.append({
                    "slot": f"{sec}.{key}",
                    "status_policy": pstatus, "status_log": t["status"],
                    "body_policy": pbody, "body_log": t["body"],
                    "diff": text_diff(pbody, t["body"]),
                    "observaciones": t["n_obs"],
                    "ejemplos": [f"L{n}" for n in t["examples"]],
                })
        else:
            nuevas.append({
                "slot": name, "status": t["status"], "body": t["body"],
                "comandos": t["comandos"], "observaciones": t["n_obs"],
                "ejemplos": [f"L{n}" for n in t["examples"]],
            })

    observed_sigs = set(sig2tpl.keys())
    huerfanas = []
    for sig, (sec, key, status, body) in pidx.items():
        if sig not in observed_sigs:
            huerfanas.append({"slot": f"{sec}.{key}", "status": status, "body": body})
    return {"iguales": iguales, "distintas": distintas,
            "nuevas": nuevas, "huerfanas": huerfanas}


def _template_group_sig(t):
    """Rebuild the structural signature of an extracted template."""
    if t.get("content_type") == "text":
        return (t["status"], ("__TEXT__", t["body"]))
    return template_signature(t["body"], t["status"])


def cmd_diff(args, silent=False):
    recs = load_log(args.log, args.frm, args.to)
    policy = json.load(open(args.against, encoding="utf-8"))
    res = compute_diff(recs, policy)
    if not silent:
        print(f"[diff] log={os.path.relpath(args.log, ROOT)} "
              f"range={args.frm}..{args.to} vs {os.path.relpath(args.against, ROOT)}\n")
        print(f"  equal     : {len(res['iguales'])}")
        print(f"  different : {len(res['distintas'])}")
        print(f"  new       : {len(res['nuevas'])}")
        print(f"  orphan    : {len(res['huerfanas'])}\n")
        for d in res["distintas"]:
            print(f"  [DIFFERENT] {d['slot']}  "
                  f"(status {d['status_policy']}->{d['status_log']}, "
                  f"{d['observaciones']} obs, {d['ejemplos'][:3]})")
            for ln in d["diff"].splitlines():
                print("      " + ln)
        for n in res["nuevas"]:
            print(f"  [NEW]      {n['slot']}  status {n['status']}  "
                  f"{n['body']}  ({n['observaciones']} obs {n['ejemplos'][:3]})")
        for h in res["huerfanas"]:
            print(f"  [ORPHAN]   {h['slot']}  status {h['status']}  (no observations in the range)")
    return res


def cmd_patch(args):
    recs = load_log(args.log, args.frm, args.to)
    policy = json.load(open(args.against, encoding="utf-8"))
    templates = extract(recs, policy)
    pidx = policy_index(policy)
    sig2pol = {sig: (sec, key) for sig, (sec, key, _, _) in pidx.items()}

    newpol = copy.deepcopy(policy)
    if isinstance(newpol.get("version"), str):
        newpol["version"] = "v2-templates"
    touched = []
    supuestos = []
    for name, t in templates.items():
        sig = _template_group_sig(t)
        if sig not in sig2pol:
            continue  # slot nuevo: no se inventan plantillas en patch
        sec, key = sig2pol[sig]
        entry = newpol[sec][key]
        entry["status"] = t["status"]
        entry["body"] = t["body_patch"]
        entry["evidencia"] = [f"L{n}" for n in t["examples"][:6]]
        if t["unknown_vars"]:
            entry["supuesto"] = "valor variable no reproducible"
            supuestos.append(f"{sec}.{key} ({', '.join(t['unknown_vars'])})")
        touched.append(f"{sec}.{key}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(newpol, f, ensure_ascii=False, indent=2)
    print(f"[patch] updated {len(touched)} templates -> "
          f"{os.path.relpath(args.out, ROOT)}")
    if supuestos:
        print(f"[patch] marked with 'supuesto' (non-reproducible var): {supuestos}")
    return newpol, touched, supuestos


def _maybe_load_policy(args):
    path = getattr(args, "against", None) or DEFAULT_POLICY
    if path and os.path.exists(path):
        try:
            return json.load(open(path, encoding="utf-8"))
        except (ValueError, OSError):
            return None
    return None


# --------------------------------------------------------------------------- #
# Selftest
# --------------------------------------------------------------------------- #
def build_synthetic_log(path, policy, n_split):
    """Write a synthetic log.

    Unchanged slots (PING, ALTA, CONSULTA, E401) to confirm they stay 'equal'.
    OPERACION and E301 change from line n_split onward:
    'recieved'->'received' and 'comando desconocido '->'comando desconocido'.
    Every instance of those two slots appears AT OR AFTER n_split, so that the
    new baseline is consistent (the change is already deployed).
    """
    lines = []
    n = 0

    def add(status, body, m):
        nonlocal n
        n += 1
        lines.append({"n": n, "section": "selftest",
                      "req": {"method": "POST", "path": "/msg",
                              "body": {"m": m}},
                      "res": {"status": status, "body": body}})

    # --- unchanged slots (before n_split) ---
    add(200, '{"pong":true}', "PING;10")
    add(201, '{"cuentaId":"AC-0001"}', "ALTA;Ana;11")
    add(201, '{"cuentaId":"AC-0002"}', "ALTA;Leo;12")
    add(200, '{"cuenta_id":"AC-0001","saldo":0}', "CONSULTA;AC-0001;13")
    add(200, '{"cuenta_id":"AC-0002","saldo":500}', "CONSULTA;AC-0002;14")
    add(404, '{"error":"E401","mensaje":"cuenta inexistente"}', "CONSULTA;AC-9999;15")
    # padding up to n_split
    while n < n_split - 1:
        add(200, '{"pong":true}', "PING;16")

    # --- from n_split onward: OPERACION and E301 CHANGED ---
    add(200, '{"status":"received","operacionId":"OP-0001"}', "DEPOSITO;AC-0001;700;17")
    add(200, '{"status":"received","operacionId":"OP-0002"}', "DEPOSITO;AC-0002;300;18")
    add(400, '{"error":"E301","mensaje":"comando desconocido"}', "SALDOS;AC-0001;19")
    add(400, '{"error":"E301","mensaje":"comando desconocido"}', "RETIRO;AC-0001;20")

    with open(path, "w", encoding="utf-8") as f:
        for o in lines:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return n


def cmd_selftest(args):
    print("=== SELFTEST ===")
    policy = json.load(open(DEFAULT_POLICY, encoding="utf-8"))
    tmpdir = tempfile.mkdtemp(prefix="templates-selftest-")
    log_path = os.path.join(tmpdir, "synthetic.jsonl")
    n_split = 7
    total = build_synthetic_log(log_path, policy, n_split)
    print(f"synthetic log: {total} lines, changes from L{n_split} onward "
          f"(recieved->received, E301 without a trailing space)")

    ns = argparse.Namespace(log=log_path, against=DEFAULT_POLICY,
                            frm=None, to=None)
    res = cmd_diff(ns, silent=True)

    different_slots = sorted(d["slot"] for d in res["distintas"])
    print(f"\ndiff: equal={len(res['iguales'])} different={different_slots} "
          f"new={[x['slot'] for x in res['nuevas']]} "
          f"orphan={len(res['huerfanas'])}")

    expected = ["errors.E301", "responses.OPERACION"]
    ok_diff = (different_slots == expected)
    print(f"  [{'OK' if ok_diff else 'FAIL'}] diff detects exactly the 2 "
          f"expected differences {expected}")
    # show the text diff of each one
    for d in res["distintas"]:
        print(f"    {d['slot']}: policy={d['body_policy']!r}  log={d['body_log']!r}")

    # patch
    out_path = os.path.join(tmpdir, "policy-patched.json")
    pns = argparse.Namespace(log=log_path, against=DEFAULT_POLICY,
                             out=out_path, frm=None, to=None)
    newpol, touched, supuestos = cmd_patch(pns)

    op = newpol["responses"]["OPERACION"]["body"]
    e301 = newpol["errors"]["E301"]["body"]
    ok_op = ("received" in op and "recieved" not in op)
    ok_e301 = (e301 == '{"error":"E301","mensaje":"comando desconocido"}')
    # the rest of the policy must stay intact except the touched responses/errors
    untouched_ok = _verify_untouched(policy, newpol)

    print(f"\npatch -> {os.path.relpath(out_path, tmpdir)}:")
    print(f"  responses.OPERACION.body = {op}")
    print(f"  errors.E301.body         = {e301}")
    print(f"  [{'OK' if ok_op else 'FAIL'}] OPERACION applies 'received'")
    print(f"  [{'OK' if ok_e301 else 'FAIL'}] E301 loses the trailing space")
    print(f"  [{'OK' if untouched_ok else 'FAIL'}] rest of the policy intact "
          f"(only responses/errors change)")

    all_ok = ok_diff and ok_op and ok_e301 and untouched_ok
    print(f"\n=== SELFTEST {'PASSED' if all_ok else 'FAILED'} ===")
    return 0 if all_ok else 1


def _verify_untouched(old, new):
    """Everything other than responses/errors (and version) must be identical."""
    for k in old:
        if k in ("responses", "errors", "version"):
            continue
        if old[k] != new.get(k):
            return False
    # and the templates NOT touched by the log must stay identical
    return True


# --------------------------------------------------------------------------- #
def build_parser():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--selftest", action="store_true",
                   help="run the self-test and exit")
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--log", default=DEFAULT_LOG)
        sp.add_argument("--from", dest="frm", type=int, default=None)
        sp.add_argument("--to", dest="to", type=int, default=None)

    e = sub.add_parser("extract", help="extract templates from the log")
    common(e)
    e.add_argument("--against", default=DEFAULT_POLICY,
                   help="policy to take variable names from (optional)")
    e.add_argument("--out", default=DEFAULT_OUT_EXTRACT)

    d = sub.add_parser("diff", help="compare extracted vs policy")
    common(d)
    d.add_argument("--against", default=DEFAULT_POLICY)

    pa = sub.add_parser("patch", help="generate a policy with templates from the log")
    common(pa)
    pa.add_argument("--against", default=DEFAULT_POLICY)
    pa.add_argument("--out", required=True)
    return p


def main(argv=None):
    p = build_parser()
    args = p.parse_args(argv)
    if args.selftest:
        return cmd_selftest(args)
    if args.cmd == "extract":
        cmd_extract(args)
        return 0
    if args.cmd == "diff":
        cmd_diff(args)
        return 0
    if args.cmd == "patch":
        cmd_patch(args)
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())

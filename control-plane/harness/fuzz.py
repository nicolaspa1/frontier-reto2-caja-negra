#!/usr/bin/env python3
"""Differential fuzzing batches shaped like the hidden evaluation set.

Generates a NEW batch of hidden-like cases from a fresh seed (singles that vary
command spelling/case/arity/control/separators/amounts/ids/names/JSON envelope,
plus short 2-5 step and long 6-12 step sequences from a ledger model with
state interactions), records it against the ORIGINAL through record.py's
transport (every request logged by experiments/cn.py, <=5 req/s), and writes
corpus-format files to --out-dir so replay.py can diff them against the replica.

  python3 fuzz.py --seed 1 --batch 1 --out-dir corpus-fuzz/batch1 \
      [--count-singles 20 --count-short 30 --count-long 10] [--budget 300]
"""
import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import labhelp as L          # noqa: E402
import control               # noqa: E402
import record                # noqa: E402

L.MIN_INTERVAL = 0.21        # <= 5 req/s against the original for this tool

KNOWN = ["PING", "ALTA", "CONSULTA", "DEPOSITO", "TRANSFER", "ANULA", "HALT", "NOTAS"]
FORBIDDEN = ("STOP", "RESET", "SHUT", "KILL", "EXPORT")
ARITY = {"PING": 0, "ALTA": 1, "CONSULTA": 1, "DEPOSITO": 2, "TRANSFER": 3, "ANULA": 1, "HALT": 1, "NOTAS": 0}
ALPHA = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
M = control.message


def safe_cmd(c):
    u = c.upper()
    return not any(f in u for f in FORBIDDEN)


def typo(rng, word, dist):
    w = list(word)
    for _ in range(dist):
        k = rng.choice(["sub", "ins", "del"]) if len(w) > 2 else "ins"
        i = rng.randrange(len(w) + (1 if k == "ins" else 0))
        if k == "sub":
            w[i] = rng.choice(ALPHA.replace(w[i], ""))
        elif k == "ins":
            w.insert(i, rng.choice(ALPHA))
        else:
            del w[i]
    return "".join(w)


def args_for(cmd, rng, n=None):
    n = ARITY.get(cmd, 1) if n is None else n
    pool = ["AC-0001", "AC-0002", "OP-0001", "100", "Lucia", "AC-0099", "50000"]
    return [rng.choice(pool) for _ in range(n)]


def gen_single(rng):
    """Returns a step (str message or raw dict) and a hypothesis label."""
    kind = rng.choice(["typo", "typo", "case", "spaces", "arity", "control", "separator",
                       "amount", "ids", "names", "json", "json", "halt1", "notas", "misc"])
    if kind == "typo":
        while True:
            base = rng.choice(KNOWN)
            d = rng.choice([1, 1, 2, 3])
            c = typo(rng, base, d)
            if c not in KNOWN and safe_cmd(c):
                break
        a = args_for(base, rng, rng.choice([ARITY[base], ARITY[base], rng.randint(0, 3)]))
        payload = ";".join([c] + a)
        return M(payload), f"typo d={d} of {base}: {c}"
    if kind == "case":
        base = rng.choice(KNOWN)
        c = "".join(ch.lower() if rng.random() < 0.5 else ch for ch in base)
        a = args_for(base, rng)
        if base == "HALT":
            a = ["AC-0001"]
        payload = ";".join([c] + a)
        return M(payload), f"mixed case {c}"
    if kind == "spaces":
        base = rng.choice(["PING", "ALTA", "CONSULTA", "DEPOSITO"])
        a = args_for(base, rng)
        payload = ";".join([base] + a)
        v = rng.choice(["lead", "trail-cmd", "lead-arg", "trail-all", "inner"])
        if v == "lead":
            m = " " + M(payload)
        elif v == "trail-cmd":
            m = M(payload.replace(base, base + " ", 1))
        elif v == "lead-arg" and a:
            m = M(payload.replace(";" + a[0], "; " + a[0], 1))
        elif v == "trail-all":
            m = M(payload) + " "
        else:
            m = M(payload) + "\t"
        return m, f"spaces {v}"
    if kind == "arity":
        base = rng.choice(KNOWN)
        n = rng.choice([x for x in range(0, 5) if x != ARITY[base]])
        if base == "HALT":
            n = rng.choice([0, 2, 3])
            if n >= 2:
                # HALT with 2+ args is never sent: the second arg could be read as confirmation
                base = "NOTAS"
                n = rng.choice([1, 2])
        payload = ";".join([base] + args_for(base, rng, n))
        return M(payload), f"arity {n} for {base}"
    if kind == "control":
        base = rng.choice(["ALTA;Lucia", "CONSULTA;AC-0001", "DEPOSITO;AC-0001;100", "PING", "NOTAS", "HALT;AC-0001"])
        ok = control.control(base)
        form = rng.choice(["05", "00", "+5", "123", "letters", "neg", "float", "space", "ok3", "zero-pad-ok", "97", "98", "99", "wrong"])
        c = {"05": "05", "00": "00", "+5": "+5", "123": "123", "letters": rng.choice(["XX", "5X", "X5", "ab"]),
             "neg": "-5", "float": "5.0", "space": f" {ok}", "ok3": f"{ok:03d}", "zero-pad-ok": f"{ok:02d}",
             "97": "97", "98": "98", "99": "99", "wrong": str((ok % 96) + 1)}[form]
        return f"{base};{c}", f"control form {form} on {base.split(';')[0]}"
    if kind == "separator":
        base = rng.choice(["PING", "ALTA;Lucia", "CONSULTA;AC-0001", "DEPOSITO;AC-0001;100"])
        v = rng.choice([",", ";;", "trailing;", "leading;", "|", "double-ctrl", ":"])
        m = M(base)
        if v == ",":
            m = m.replace(";", ",")
        elif v == ";;":
            m = m.replace(";", ";;", 1)
        elif v == "trailing;":
            m = m + ";"
        elif v == "leading;":
            m = ";" + m
        elif v == "|":
            m = m.replace(";", "|")
        elif v == ":":
            m = m.replace(";", ":")
        else:
            m = m + ";" + m.rsplit(";", 1)[1]
        return m, f"separator {v}"
    if kind == "amount":
        amt = rng.choice(["0", "-1", "1", "99999", "100000", "100001", "9999999999", "10000000000", "1e5", "1.5",
                          " 5", "５", "+100", "007", "1_000", "100 ", "0x10", "１００", "999999999999999999999"])
        cmd = rng.choice(["DEPOSITO;AC-0001;" + amt, "TRANSFER;AC-0001;AC-0002;" + amt])
        return M(cmd), f"amount {amt!r}"
    if kind == "ids":
        i = rng.choice(["AC-0000", "AC-9999", "ac-0001", "AC-1", "AC-00001", "AC_0001", "AC0001", "AC-0001 ", "OP-0000",
                        "OP-9999", "op-0001", "OP-1", "AC-0001", "OP-0001", "ＡＣ-0001"])
        if i.upper().startswith("OP") or i.startswith("ＯＰ"):
            cmd = f"ANULA;{i}"
        else:
            cmd = rng.choice([f"CONSULTA;{i}", f"DEPOSITO;{i};100", f"TRANSFER;{i};AC-0002;100", f"TRANSFER;AC-0002;{i};100"])
        return M(cmd), f"id {i!r}"
    if kind == "names":
        nm = rng.choice(["", " ", "  ", "Juan Perez", "José", "Ñandú", "Ana😀", "李雷", "A" * 200, "A" * 1000, "123",
                         "O'Brien", 'Q"uote', "a\\b", "tab\there", "x;y", "Lucia-Gomez", "{\"m\":1}", "null", "-"])
        payload = f"ALTA;{nm}"
        return M(payload), f"name {nm[:20]!r} len={len(nm)}"
    if kind == "json":
        v = rng.choice(['{"m":""}', '{"M":"PING;64"}', '{"m":null}', '{"m":5}', '{"m":true}', '{"m":["PING;64"]}',
                        '{"m":{"m":"PING;64"}}', '{"m":"PING;64","x":1}', '{"x":1,"m":"PING;64"}', '{"m":"PING;64"} ',
                        ' {"m":"PING;64"}', '{"m":"PING;64"}{"m":"PING;64"}', '{"m":"PING;64",}', "{'m':'PING;64'}",
                        '', ' ', 'null', '"PING;64"', '123', '[]', '[{"m":"PING;64"}]', '{"m" : "PING;64"}',
                        '{"m":"PING;64"}\n', '{"m":"PING\\u003b64"}', '{"m":"PING;64","m":"ALTA;Lucia;82"}',
                        '{"m":"\\u0050ING;64"}', 'BIG8192', 'BIG8193', 'BIG8191', 'BIG4096', 'BIG16384', 'BIGM8193'])
        if v.startswith("BIG"):
            n = int(v[4:]) if v.startswith("BIGM") else int(v[3:])
            if v.startswith("BIGM"):
                # exactly n bytes of valid JSON whose m is a valid PING message plus padding spaces outside m
                core = '{"m":"PING;64"}'
                body = core[:-1] + ' ' * (n - len(core)) + '}'
            else:
                prefix, suffix = '{"m":"', '"}'
                body = prefix + "A" * (n - len(prefix) - len(suffix)) + suffix
            return {"raw": {"method": "POST", "path": "/msg", "body": body}}, f"json body {v}"
        return {"raw": {"method": "POST", "path": "/msg", "body": v}}, f"json variant {v[:30]!r}"
    if kind == "halt1":
        # Only argument values already observed to answer 409 E520 (L894, L1355, L2046): "1" is a CONFIRMATION
        # that halts the original (L2272) and must never be sent again.
        a = rng.choice(["AC-0001", "x", "no", "OP-0001", "AC-9999"])
        return M(f"HALT;{a}"), f"HALT one arg {a!r}"
    if kind == "notas":
        return rng.choice(["NOTAS;11", "notas;11", "NOTAS;XX", "NOTAS;1", "NOTAS", "NOTAS;"]), "NOTAS variants"
    # misc
    return rng.choice([";", ";;", ";64", "PING;64;", "64", "PING;;64", "PING; 64", " ;64", " PING;64", "PING;６４",
                       "ＰＩＮＧ;64", "PING;64\n", "P;64", "A;Lucia;82", ";;;", "CONSULTA;AC-0001;14;", "PING;64;64;64"]), "misc"


# ----------------------------------------------------------------- sequences
NAMES = ["Lucia", "Mateo", "Ana", "Juan Perez", "X1", "Sofia", "123", "José", "Lu", "Pedro Pablo"]


class Model:
    """Loose ledger model: tracks ids so sequences reference plausible accounts/ops."""
    def __init__(self):
        self.accts = []
        self.ops = []
        self.bal = {}
        self.strikes = {}
        self.blocked = set()

    def ac(self, rng, missing_p=0.1):
        if not self.accts or rng.random() < missing_p:
            return f"AC-{rng.randint(len(self.accts) + 1, len(self.accts) + 3):04d}"
        return rng.choice(self.accts)

    def op(self, rng, missing_p=0.15):
        if not self.ops or rng.random() < missing_p:
            return f"OP-{rng.randint(len(self.ops) + 1, len(self.ops) + 3):04d}"
        return rng.choice(self.ops)


def gen_sequence(rng, nsteps):
    mdl = Model()
    steps = []

    def alta():
        mdl.accts.append(f"AC-{len(mdl.accts) + 1:04d}")
        mdl.bal[mdl.accts[-1]] = 0
        mdl.strikes[mdl.accts[-1]] = 0
        return M(f"ALTA;{rng.choice(NAMES)}")

    def op_ok():
        mdl.ops.append(f"OP-{len(mdl.ops) + 1:04d}")

    for _ in range(rng.randint(1, 2)):
        steps.append(alta())
    while len(steps) < nsteps:
        kind = rng.choices(
            ["alta", "deposito", "deposito_bad", "consulta", "transfer_ok", "transfer_limit", "transfer_over",
             "transfer_e507", "transfer_same", "transfer_missing", "anula", "anula_twice", "ping", "notas", "halt",
             "unknown", "bad_control", "bad_arity", "empty_seg", "raw"],
            [3, 5, 1, 4, 4, 2, 4, 2, 1, 1, 4, 2, 1, 1, 1, 1, 1, 1, 1, 1])[0]
        if kind == "alta":
            steps.append(alta())
        elif kind == "deposito":
            a = mdl.ac(rng)
            amt = rng.choice([1, 100, 700, 50000, 100000, 100001, 250000, 1000000, 9999999999])
            steps.append(M(f"DEPOSITO;{a};{amt}"))
            if a in mdl.bal:
                mdl.bal[a] += amt
                op_ok()
        elif kind == "deposito_bad":
            a = mdl.ac(rng)
            steps.append(M(f"DEPOSITO;{a};{rng.choice(['0', '-1', '01', '1.5', 'x', '10000000000', ' 5', '1e5'])}"))
        elif kind == "consulta":
            steps.append(M(f"CONSULTA;{mdl.ac(rng, 0.2)}"))
        elif kind.startswith("transfer"):
            if len(mdl.accts) < 2:
                steps.append(alta())
                continue
            o, d = rng.sample(mdl.accts, 2)
            if kind == "transfer_ok":
                amt = rng.choice([1, 100, 500, 40000, 99999, 100000])
                if mdl.bal[o] < amt and rng.random() < 0.6:
                    steps.append(M(f"DEPOSITO;{o};{amt * 2}"))
                    mdl.bal[o] += amt * 2
                    op_ok()
                    if len(steps) >= nsteps:
                        break
            elif kind == "transfer_limit":
                amt = rng.choice([100000, 100001])
            elif kind == "transfer_over":
                amt = rng.choice([100001, 100002, 120000, 200000, 9999999999])
            elif kind == "transfer_e507":
                amt = rng.choice([1, 100, 99999])
                d2 = d
                o = rng.choice([x for x in mdl.accts if mdl.bal[x] == 0] or [o])
                d = d2 if d2 != o else [x for x in mdl.accts if x != o][0]
            elif kind == "transfer_same":
                d = o
                amt = rng.choice([100, 100001, 0])
            else:
                d = f"AC-{len(mdl.accts) + rng.randint(1, 3):04d}"
                amt = rng.choice([100, 200000])
            steps.append(M(f"TRANSFER;{o};{d};{amt}"))
            if o != d and d in mdl.bal and o not in mdl.blocked:
                if amt > 100000 or mdl.bal[o] < amt:
                    mdl.strikes[o] += 1
                    if mdl.strikes[o] >= 3:
                        mdl.blocked.add(o)
                else:
                    mdl.bal[o] -= amt
                    mdl.bal[d] += amt
                    mdl.strikes[o] = 0
                    op_ok()
        elif kind == "anula":
            steps.append(M(f"ANULA;{mdl.op(rng)}"))
        elif kind == "anula_twice":
            o = mdl.op(rng, 0.0)
            steps.append(M(f"ANULA;{o}"))
            if len(steps) < nsteps:
                steps.append(rng.choice([M(f"ANULA;{o}"), M(f"DEPOSITO;{mdl.ac(rng, 0)};100"), M(f"CONSULTA;{mdl.ac(rng, 0)}")]))
        elif kind == "ping":
            steps.append(rng.choice(["PING;64", "ping;64", "PING;XX", "PING;1", "PING"]))
        elif kind == "notas":
            steps.append(rng.choice(["NOTAS;11", "NOTAS;XX", M("NOTAS;x")]))
        elif kind == "halt":
            steps.append(M(f"HALT;{rng.choice(['AC-0001', 'x', 'no'])}"))  # ONE non-confirming argument, always (never "1": L2272)
        elif kind == "unknown":
            while True:
                c = typo(rng, rng.choice(KNOWN), rng.choice([1, 2, 3]))
                if c not in KNOWN and safe_cmd(c):
                    break
            steps.append(M(";".join([c] + args_for(c, rng, rng.randint(0, 2)))))
        elif kind == "bad_control":
            p = rng.choice([f"CONSULTA;{mdl.ac(rng, 0)}", f"DEPOSITO;{mdl.ac(rng, 0)};100", f"ALTA;{rng.choice(NAMES)}"])
            ok = control.control(p)
            steps.append(f"{p};{rng.choice(['XX', '+5', str((ok % 96) + 1), f'{ok:03d}', '00'])}")
        elif kind == "bad_arity":
            steps.append(M(rng.choice([f"DEPOSITO;{mdl.ac(rng, 0)}", "TRANSFER;AC-0001;AC-0002", "CONSULTA", "ALTA",
                                       "ANULA;OP-0001;x", "PING;x", f"ALTA;{rng.choice(NAMES)};x"])))
        elif kind == "empty_seg":
            steps.append(rng.choice([M(f"DEPOSITO;{mdl.ac(rng, 0)};"), M("ALTA;"), M("TRANSFER;AC-0001;;100"), "CONSULTA;AC-0001;"]))
        else:
            steps.append({"raw": {"method": "POST", "path": "/msg",
                                  "body": rng.choice(['{}', '{"m":null}', '{', '{"m":"PING;64","x":1}', '[]', ''])}})
    return steps[:nsteps]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--count-singles", type=int, default=20)
    ap.add_argument("--count-short", type=int, default=30)
    ap.add_argument("--count-long", type=int, default=10)
    ap.add_argument("--budget", type=int, default=320)
    ap.add_argument("--dry-run", action="store_true", help="print the generated cases, send nothing")
    args = ap.parse_args()
    rng = random.Random(args.seed)
    out = args.out_dir if os.path.isabs(args.out_dir) else os.path.join(L.ROOT, args.out_dir)
    os.makedirs(out, exist_ok=True)
    tag = f"fuzz-b{args.batch}"

    plan = []
    for i in range(1, args.count_singles + 1):
        st, hip = gen_single(rng)
        plan.append((f"{tag}-single-{i:02d}", f"fuzz single: {hip}", [st], "singles"))
    for i in range(1, args.count_short + 1):
        n = rng.randint(2, 5)
        plan.append((f"{tag}-short-{i:02d}", f"fuzz short {n} steps (seed {args.seed})", gen_sequence(rng, n), "short"))
    for i in range(1, args.count_long + 1):
        n = rng.randint(6, 12)
        plan.append((f"{tag}-long-{i:02d}", f"fuzz long {n} steps (seed {args.seed})", gen_sequence(rng, n), "long"))

    # safety: never a HALT with 2+ args, never forbidden-looking commands
    for cid, _, steps, _ in plan:
        for st in steps:
            if isinstance(st, str):
                parts = st.split(";")
                assert not (parts[0].strip().upper() == "HALT" and (len(parts) != 3 or parts[1].strip() in ("1", "SI", "si", "yes", "true", "Y"))), (cid, st)
                assert safe_cmd(parts[0]), (cid, st)
    total = sum(len(s) + 1 for _, _, s, _ in plan)
    print(f"[fuzz] batch {args.batch} seed {args.seed}: {len(plan)} cases, {total} requests planned")
    if args.dry_run:
        for cid, hip, steps, _ in plan:
            print(cid, hip, steps)
        return

    budget = record.Budget(args.budget)
    groups = {"singles": [], "short": [], "long": []}
    try:
        for cid, hip, steps, g in plan:
            groups[g].append(record.run_case(budget, cid, hip, steps, f"{tag}:{hip[:48]}"))
    finally:
        for g, cases in groups.items():
            if cases:
                record.write_cases(os.path.join(out, f"{g}.jsonl"), cases)
        print(f"[fuzz] requests spent in this run: {budget.spent()}")


if __name__ == "__main__":
    main()

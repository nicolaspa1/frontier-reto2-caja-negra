#!/usr/bin/env python3
"""Run validation/hidden-like.jsonl against a deployed replica (validator's runner).

Rules mirror the hidden evaluator: POST /__reset before every case, each step
compared with harness/compare.py (same status; JSON bodies key-order-insensitive,
otherwise exact text). Latency measured per step (5 s budget). Several passes
detect nondeterminism (same step, different observed response across passes).

  python3 validation/run_validation.py --target URL [--passes 3] [--cases validation/hidden-like.jsonl]
Token: TEAM_TOKEN from .env (never printed).
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "harness"))
import compare as C  # noqa: E402


def read_token():
    for line in open(os.path.join(os.path.dirname(ROOT), ".env")):  # .env lives at the repository root
        line = line.strip()
        if line.startswith("TEAM_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("TEAM_TOKEN not found in .env")


class Client:
    def __init__(self, base, token):
        self.base = base.rstrip("/")
        self.token = token
        self.lat = []        # (case_id, step_no, seconds)

    def send(self, method, path, body=None, auth="valid", headers=None, tag=("?", 0)):
        h = {}
        if auth == "valid":
            h["Authorization"] = f"Bearer {self.token}"
        elif auth == "lowercase":
            h["Authorization"] = f"bearer {self.token}"
        elif auth == "wrong":
            h["Authorization"] = "Bearer ftr_not_the_team_token"
        elif auth == "noscheme":
            h["Authorization"] = self.token
        data = None
        if body is not None:
            data = body.encode("utf-8")
            h.setdefault("Content-Type", "application/json")
        if headers:
            h.update(headers)
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        if method == "POST" and data is None:
            req.data = b""
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                st, txt = r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            st, txt = e.code, e.read().decode("utf-8", "replace")
        except Exception as e:  # timeout / connection error
            st, txt = 0, f"<client-error {type(e).__name__}: {e}>"
        dt = time.perf_counter() - t0
        self.lat.append((tag[0], tag[1], dt))
        return st, txt


def run_pass(cases, cli):
    out = {}
    for c in cases:
        cid = c["case_id"]
        rst = cli.send("POST", "/__reset", tag=(cid, 0))
        steps = []
        for i, s in enumerate(c["steps"], 1):
            if "m" in s:
                st, body = cli.send("POST", "/msg", json.dumps({"m": s["m"]}, ensure_ascii=False), tag=(cid, i))
            else:
                r = s["raw"]
                st, body = cli.send(r["method"], r["path"], r.get("body"), r.get("auth", "valid"), r.get("headers"), tag=(cid, i))
            ok, reason = C.compare({"status": s["expected_status"], "body": s["expected_body"]}, {"status": st, "body": body})
            steps.append({"ok": ok, "status": st, "body": body, "reason": None if ok else reason})
        out[cid] = {"reset": rst, "steps": steps}
    return out


def pct(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True)
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--cases", default=os.path.join(ROOT, "validation", "hidden-like.jsonl"))
    ap.add_argument("--out", default=os.path.join(ROOT, "validation", "results.json"))
    ap.add_argument("--token", default=None, help="override TEAM_TOKEN from .env (e.g. a local instance)")
    a = ap.parse_args()

    cases = [json.loads(l) for l in open(a.cases, encoding="utf-8") if l.strip()]
    cli = Client(a.target, a.token or read_token())
    passes = []
    for p in range(a.passes):
        t0 = time.time()
        res = run_pass(cases, cli)
        passes.append(res)
        n_steps = sum(len(v["steps"]) for v in res.values())
        ok_steps = sum(1 for v in res.values() for s in v["steps"] if s["ok"])
        ok_cases = sum(1 for v in res.values() if all(s["ok"] for s in v["steps"]))
        print(f"pass {p + 1}: cases {ok_cases}/{len(cases)}  steps {ok_steps}/{n_steps}  ({time.time() - t0:.1f}s)")

    # ---- failures (pass 1) and nondeterminism across passes
    by_case = {c["case_id"]: c for c in cases}
    failures = []
    for pno, pres in enumerate(passes, 1):
        for cid, v in pres.items():
            for i, s in enumerate(v["steps"]):
                if not s["ok"]:
                    spec = by_case[cid]["steps"][i]
                    failures.append({"pass": pno, "case_id": cid, "step": i + 1, "kind": by_case[cid]["kind"],
                                     "request": spec.get("m", spec.get("raw")), "source": spec["source"],
                                     "expected": [spec["expected_status"], spec["expected_body"]],
                                     "observed": [s["status"], s["body"]]})
    nondet = []
    for cid in passes[0]:
        for i in range(len(passes[0][cid]["steps"])):
            obs = {(p[cid]["steps"][i]["status"], p[cid]["steps"][i]["body"]) for p in passes}
            if len(obs) > 1:
                nondet.append({"case_id": cid, "step": i + 1, "observed": sorted(obs)})

    lat = sorted(d for _, i, d in cli.lat if i > 0)
    lat_reset = sorted(d for _, i, d in cli.lat if i == 0)
    slow = [(cid, i, round(d, 3)) for cid, i, d in cli.lat if d >= 5.0]
    http5xx = sum(1 for p in passes for v in p.values() for s in v["steps"] if s["status"] >= 500)
    client_err = sum(1 for p in passes for v in p.values() for s in v["steps"] if s["status"] == 0)

    per_kind = {}
    for k in ("single", "short", "long"):
        ids = [c["case_id"] for c in cases if c["kind"] == k]
        per_kind[k] = {"cases": len(ids),
                       "pass": [sum(1 for cid in ids if all(s["ok"] for s in p[cid]["steps"])) for p in passes]}
    summary = {
        "target": a.target, "passes": a.passes, "n_cases": len(cases),
        "n_steps": sum(len(c["steps"]) for c in cases),
        "cases_pass": [sum(1 for v in p.values() if all(s["ok"] for s in v["steps"])) for p in passes],
        "steps_pass": [sum(1 for v in p.values() for s in v["steps"] if s["ok"]) for p in passes],
        "per_kind": per_kind,
        "latency_ms": {"n": len(lat), "p50": round(pct(lat, .5) * 1000), "p90": round(pct(lat, .9) * 1000),
                       "p99": round(pct(lat, .99) * 1000), "max": round(lat[-1] * 1000) if lat else 0,
                       "reset_p50": round(pct(lat_reset, .5) * 1000), "reset_max": round(lat_reset[-1] * 1000) if lat_reset else 0},
        "steps_over_5s": slow, "http_5xx": http5xx, "client_errors": client_err,
        "nondeterministic_steps": nondet, "failures": failures,
    }
    json.dump(summary, open(a.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps({k: summary[k] for k in ("cases_pass", "steps_pass", "per_kind", "latency_ms", "http_5xx",
                                               "client_errors")}, ensure_ascii=False))
    print(f"steps >=5s: {len(slow)}   nondeterministic steps: {len(nondet)}   failures(all passes): {len(failures)}")
    for f in failures:
        print(f"  FAIL p{f['pass']} {f['case_id']} step {f['step']} [{f['source']}] req={json.dumps(f['request'], ensure_ascii=False)[:90]}\n"
              f"       expected {f['expected'][0]} {f['expected'][1][:120]}\n       observed {f['observed'][0]} {f['observed'][1][:120]}")
    for n in nondet:
        print(f"  NONDET {n['case_id']} step {n['step']}: {n['observed']}")


if __name__ == "__main__":
    main()

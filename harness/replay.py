#!/usr/bin/env python3
"""Replay recorded cases (corpus/*.jsonl) against a server and measure fidelity.

For each case: `POST /__reset` and then every step, comparing the server's
response against the recorded expected response from the original (evaluator
rule, see compare.py). Reports the percentage of equal steps, per-case diffs
and coverage (error codes and triggered commands).

Transport:
  - If --target is the ORIGINAL service (host from .env), experiments/cn.py is
    used (every call is written to experiments/log.jsonl, throttled < 8 req/s).
  - For any other --target (e.g. the replica), httpx is used directly.

CLI:
  python3 replay.py --target URL --token T [--cases corpus/*.jsonl]
  python3 replay.py --sample 10           # random sample of N cases
  python3 replay.py --coverage            # print coverage (errors and commands)
  python3 replay.py --bench               # p50/p99 latency and 5xx count
  python3 replay.py --max-diffs 20        # how many diffs to detail
"""
import argparse
import glob
import json
import os
import random
import sys
import time
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import labhelp as L          # noqa: E402
import compare as C          # noqa: E402

ROOT = L.ROOT


class CnTransport:
    """Against the ORIGINAL via cn.py (logged + throttled)."""
    name = "original(cn)"

    def __init__(self):
        self.timings = []

    def reset(self, section=""):
        t0 = time.time()
        r = L.og_reset(section=section)
        self.timings.append(time.time() - t0)
        return r

    def msg(self, m, section=""):
        t0 = time.time()
        r = L.og_msg(m, section=section)
        self.timings.append(time.time() - t0)
        return r

    def raw(self, method, path, body=None, section=""):
        t0 = time.time()
        r = L.og_raw(method, path, raw_body=body, section=section)
        self.timings.append(time.time() - t0)
        return r


class HttpxTransport:
    """Against any target (replica) via httpx directly."""
    name = "httpx"

    def __init__(self, target, token):
        import httpx
        self.base = target.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=self.base, headers=headers, timeout=15.0)
        self.timings = []

    def _do(self, method, path, body=None, raw=None):
        h = {}
        data = None
        if raw is not None:
            data = raw.encode()
            h["Content-Type"] = "application/json"
        elif body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        t0 = time.time()
        resp = self.client.request(method, path, content=data, headers=h)
        self.timings.append(time.time() - t0)
        return resp.status_code, resp.text

    def reset(self, section=""):
        return self._do("POST", "/__reset")

    def msg(self, m, section=""):
        return self._do("POST", "/msg", body={"m": m})

    def raw(self, method, path, body=None, section=""):
        return self._do(method, path, raw=body)


def is_original(target):
    return urlparse(target).netloc == urlparse(L.ORIGINAL_URL).netloc


def load_cases(patterns):
    cases = []
    for pat in patterns:
        for path in sorted(glob.glob(pat)):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        c = json.loads(line)
                        c["_file"] = os.path.basename(path)
                        cases.append(c)
    return cases


def cmd_of(step):
    if "m" in step and isinstance(step["m"], str) and step["m"]:
        return step["m"].split(";", 1)[0].upper()
    if "raw" in step:
        return f"RAW {step['raw'].get('method')} {step['raw'].get('path')}"
    return "?"


def err_of(body):
    try:
        o = json.loads(body)
        if isinstance(o, dict) and "error" in o:
            return o["error"]
    except (ValueError, TypeError):
        pass
    return None


def run(cases, transport, max_diffs):
    total_steps = equal_steps = 0
    case_pass = 0
    http5xx = 0
    diffs = []
    cov_errors = {}
    cov_cmds = {}
    for c in cases:
        section = f"replay:{c.get('case_id', '?')}"
        transport.reset(section=section)
        ok_case = True
        for i, step in enumerate(c["steps"], 1):
            exp = {"status": step.get("status"), "body": step.get("body")}
            # setup steps without a recorded status are skipped from comparison
            if exp["status"] is None:
                if "m" in step:
                    transport.msg(step["m"], section=section)
                continue
            if "raw" in step:
                r = step["raw"]
                status, body = transport.raw(r["method"], r["path"], r.get("body"), section=section)
            else:
                status, body = transport.msg(step["m"], section=section)
            total_steps += 1
            if isinstance(status, int) and status >= 500:
                http5xx += 1
            cov_cmds[cmd_of(step)] = cov_cmds.get(cmd_of(step), 0) + 1
            e = err_of(step.get("body"))
            if e:
                cov_errors[e] = cov_errors.get(e, 0) + 1
            ok, reason = C.compare(exp, {"status": status, "body": body})
            if ok:
                equal_steps += 1
            else:
                ok_case = False
                if len(diffs) < max_diffs:
                    diffs.append((c.get("case_id"), i, cmd_of(step), reason))
        if ok_case:
            case_pass += 1
    return {
        "total_steps": total_steps, "equal_steps": equal_steps,
        "case_pass": case_pass, "n_cases": len(cases), "http5xx": http5xx,
        "diffs": diffs, "cov_errors": cov_errors, "cov_cmds": cov_cmds,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default=L.ORIGINAL_URL)
    ap.add_argument("--token", default=None, help="Bearer token (httpx only)")
    ap.add_argument("--cases", nargs="*", default=[os.path.join(ROOT, "corpus", "*.jsonl")])
    ap.add_argument("--sample", type=int, default=0, help="random sample of N cases")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--bench", action="store_true")
    ap.add_argument("--max-diffs", type=int, default=20)
    args = ap.parse_args()

    cases = load_cases(args.cases)
    if args.sample and args.sample < len(cases):
        random.Random(args.seed).shuffle(cases)
        cases = cases[:args.sample]
    if not cases:
        print("[replay] no cases to replay", file=sys.stderr)
        sys.exit(2)

    if is_original(args.target):
        transport = CnTransport()
        print(f"[replay] target=ORIGINAL ({args.target}) via cn.py (logged)")
    else:
        transport = HttpxTransport(args.target, args.token)
        print(f"[replay] target={args.target} via httpx")

    res = run(cases, transport, args.max_diffs)
    pct = 100.0 * res["equal_steps"] / res["total_steps"] if res["total_steps"] else 0.0
    print(f"\n[replay] cases={res['n_cases']}  steps={res['total_steps']}  "
          f"equal={res['equal_steps']} ({pct:.2f}%)  "
          f"cases 100%={res['case_pass']}/{res['n_cases']}")

    if res["diffs"]:
        print(f"\n[replay] diffs (max {args.max_diffs}):")
        for cid, i, cmd, reason in res["diffs"]:
            # NOTE: the token "paso" is kept on purpose: record.py --from-diff
            # parses this exact line format (see record.parse_diff_report).
            print(f"   {cid} paso {i} [{cmd}]: {reason}")

    if args.coverage:
        print("\n[replay] error-code coverage:")
        for k in sorted(res["cov_errors"]):
            print(f"   {k}: {res['cov_errors'][k]}")
        print("[replay] triggered-command coverage:")
        for k in sorted(res["cov_cmds"]):
            print(f"   {k}: {res['cov_cmds'][k]}")

    if args.bench:
        ts = sorted(transport.timings)
        if ts:
            def pctile(p):
                return ts[min(len(ts) - 1, int(p * len(ts)))]
            print("\n[replay] latency (s): "
                  f"n={len(ts)} p50={pctile(0.50)*1000:.0f}ms "
                  f"p99={pctile(0.99)*1000:.0f}ms max={ts[-1]*1000:.0f}ms")
        print(f"[replay] 5xx responses observed: {res['http5xx']}")

    # exit code: 0 if 100% of steps are equal
    sys.exit(0 if res["equal_steps"] == res["total_steps"] else 1)


if __name__ == "__main__":
    main()

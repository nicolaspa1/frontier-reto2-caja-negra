#!/usr/bin/env python3
"""Laboratory probe runner: harness/sondas.json against a --target (never the original).

Per probe: POST /__reset, then its `mensajes` in order. Message formats:
  - string                      -> POST /msg {"m": <string>}  (control included)
  - {"m": ..., "token": ...}    -> POST /msg with Authorization Bearer <token>
  - {"raw": true, "method","path","auth","body"?,"headers"?,"bytes"?}
        auth=true adds the team token; "<token>" inside a header is replaced by it;
        a string body is sent as-is, an object body is serialized to JSON.

The first run against a target stores lab/probes_baseline_<label>.jsonl; later
runs compare status+body (compare.py) and print the probes whose result changed
with their `señal_de_cambio`. Latency (p50/p99/max, >5000 ms, 5xx) goes to
harness/metrics.jsonl, one line per run. INFRA responses (X-Frontier-Infra,
502/503/504, "Application not found") are counted apart and retried once after
--infra-wait seconds.

CLI:
  python3 probes.py --target http://localhost:8091 --token lab            # ronda_larga
  python3 probes.py --target http://localhost:8091 --token lab --all      # all
  python3 probes.py --target http://localhost:8092 --token lab --baseline # force baseline
  python3 probes.py --target ... --token ... --sondas A1,A2               # subset
"""
import argparse
import json
import os
import sys
import time
from time import perf_counter
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compare as C          # noqa: E402

ROOT = os.path.dirname(HERE)
SONDAS_FILE = os.path.join(HERE, "sondas.json")
LAB = os.path.join(ROOT, "lab")
METRICS_FILE = os.path.join(HERE, "metrics.jsonl")


def is_infra(status, headers, body):
    """INFRA response (not the service): X-Frontier-Infra, 502/503/504, or a
    body containing 'Application not found'."""
    hl = {k.lower(): v for k, v in (headers or {}).items()}
    if "x-frontier-infra" in hl:
        return True
    if status in (502, 503, 504):
        return True
    if body and "Application not found" in body:
        return True
    return False


def write_metric(target, steps, equal, http, alerts):
    """One line per run in harness/metrics.jsonl; keys are frozen (same format as the monitor)."""
    p50, p99, mx = http.pcts()
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target": target,
        "fuente": "probes",
        "pasos": steps,
        "iguales": equal,
        "p50": p50,
        "p99": p99,
        "max": mx,
        "n_lentas": http.n_slow,
        "n_5xx": http.n_5xx,
        "n_infra": http.n_infra,
        "requests_gastados": http.n,
        "alertas": alerts,
    }
    with open(METRICS_FILE, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def target_label(target):
    u = urlparse(target)
    return (u.netloc or target).replace(":", "_").replace("/", "_")


def baseline_path(target):
    return os.path.join(LAB, f"probes_baseline_{target_label(target)}.jsonl")


def load_sondas(select_all, only, sondas_file=None):
    data = json.load(open(sondas_file or SONDAS_FILE))
    sondas = data["sondas"]
    if only:
        wanted = set(only)
        return [s for s in sondas if s.get("id") in wanted and not s.get("no_enviar")]
    if select_all:
        return sondas
    return [s for s in sondas if s.get("ronda_larga") and not s.get("no_enviar")]


class Http:
    def __init__(self, target, token, infra_wait=10):
        import httpx
        self.base = target.rstrip("/")
        self.token = token
        self.client = httpx.Client(base_url=self.base, timeout=15.0)
        self.n = 0
        self.infra_wait = infra_wait
        self.times = []
        self.n_5xx = 0
        self.n_slow = 0
        self.n_infra = 0
        self.last_infra = False

    def close(self):
        self.client.close()

    def _send(self, method, path, headers, content, _retry=True):
        self.n += 1
        t0 = perf_counter()
        try:
            resp = self.client.request(method, path, headers=headers, content=content)
            ms = (perf_counter() - t0) * 1000.0
            status, body, hdrs = resp.status_code, resp.text, dict(resp.headers)
        except Exception as e:
            # A probe the HTTP client refuses to send (e.g. a lying Content-Length) must not
            # bring down the run: record a deterministic, comparable result instead.
            ms = (perf_counter() - t0) * 1000.0
            status, body, hdrs = None, f"<client-error: {type(e).__name__}>", {}
        self.times.append(ms)
        if ms > 5000:
            self.n_slow += 1
        if isinstance(status, int) and status >= 500:
            self.n_5xx += 1
        self.last_infra = is_infra(status, hdrs, body)
        if self.last_infra:
            self.n_infra += 1
            if self.infra_wait > 0 and _retry:
                # infra response: wait and retry once
                time.sleep(self.infra_wait)
                return self._send(method, path, headers, content, _retry=False)
        return status, body

    def pcts(self):
        if not self.times:
            return 0.0, 0.0, 0.0
        s = sorted(self.times)
        p = lambda q: s[min(len(s) - 1, int(q * len(s)))]  # noqa: E731
        return round(p(0.50), 1), round(p(0.99), 1), round(s[-1], 1)

    @staticmethod
    def _gen_pad(nbytes):
        """Valid JSON body of exactly nbytes bytes: {"m":"PING;64","pad":"x..."}."""
        prefix = '{"m":"PING;64","pad":"'
        suffix = '"}'
        padlen = max(0, nbytes - len(prefix) - len(suffix))
        return prefix + ("x" * padlen) + suffix

    def step(self, m):
        """Run one message (string or dict) and return (status, body)."""
        if isinstance(m, str):
            h = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}
            return self._send("POST", "/msg", h, json.dumps({"m": m}).encode())
        if "token" in m and "raw" not in m:
            h = {"Authorization": f"Bearer {m['token']}", "Content-Type": "application/json"}
            return self._send("POST", "/msg", h, json.dumps({"m": m["m"]}).encode())
        method = m.get("method", "POST")
        path = m.get("path", "/msg")
        headers = {}
        if m.get("auth"):
            headers["Authorization"] = f"Bearer {self.token}"
        body = m.get("body")
        content = None
        # synthetic body: generar_pad+bytes => JSON padded to that exact size
        if m.get("generar_pad") and m.get("bytes"):
            content = self._gen_pad(int(m["bytes"])).encode()
            headers.setdefault("Content-Type", "application/json")
        elif body is not None:
            if isinstance(body, str):
                content = body.encode()
            else:
                content = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        # a manual Content-Length is skipped: the client computes it from the real body
        for k, v in (m.get("headers") or {}).items():
            if k.lower() == "content-length":
                continue
            if isinstance(v, str):
                v = v.replace("<token>", self.token)
            headers[k] = v
        return self._send(method, path, headers, content)


def run_sonda(http, sonda):
    http._send("POST", "/__reset", {"Authorization": f"Bearer {http.token}"}, None)
    steps = []
    for m in sonda.get("mensajes", []):
        status, body = http.step(m)
        steps.append({"status": status, "body": body})
    return steps


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", required=True, help="URL of the local emulator (not the original)")
    ap.add_argument("--token", default="lab")
    ap.add_argument("--all", action="store_true", help="all probes (not only ronda_larga)")
    ap.add_argument("--sondas", default="", help="comma-separated list of ids")
    ap.add_argument("--baseline", action="store_true", help="force (re)writing the baseline")
    ap.add_argument("--ref", default=None,
                    help="baseline file to compare against (e.g. the v1 one); "
                         "defaults to the target's own baseline")
    ap.add_argument("--sondas-file", default=None,
                    help="path to sondas.json (default harness/sondas.json); "
                         "using a fixed snapshot gives reproducible runs")
    ap.add_argument("--infra-wait", type=int, default=10,
                    help="seconds to wait and retry after an INFRA response (#4)")
    args = ap.parse_args()

    if urlparse(args.target).netloc.startswith("caja-negra"):
        print("[probes] REJECTED: the target looks like the original; forbidden.", file=sys.stderr)
        sys.exit(3)

    only = [s.strip() for s in args.sondas.split(",") if s.strip()]
    sondas = load_sondas(args.all, only, args.sondas_file)
    os.makedirs(LAB, exist_ok=True)
    bpath = baseline_path(args.target)
    ref = args.ref or bpath
    have_baseline = os.path.exists(ref) and not args.baseline

    http = Http(args.target, args.token, infra_wait=args.infra_wait)
    t0 = time.time()
    results = {}
    try:
        for s in sondas:
            if s.get("no_enviar"):
                continue
            results[s["id"]] = run_sonda(http, s)
    finally:
        http.close()
    dt = time.time() - t0
    p50, p99, mx = http.pcts()

    if not have_baseline:
        with open(bpath, "w") as f:
            for s in sondas:
                if s.get("no_enviar"):
                    continue
                f.write(json.dumps({"id": s["id"], "steps": results[s["id"]]},
                                   ensure_ascii=False) + "\n")
        total = sum(len(v) for v in results.values())
        write_metric(args.target, total, total, http, 0)
        print(f"[probes] BASELINE written: {os.path.relpath(bpath, ROOT)} "
              f"({len(sondas)} probes, {http.n} requests, {dt:.1f}s)")
        print(f"[probes] latency p50={p50}ms p99={p99}ms max={mx}ms "
              f"slow>5s={http.n_slow} 5xx={http.n_5xx} infra={http.n_infra}")
        return

    base = {}
    for line in open(ref):
        r = json.loads(line)
        base[r["id"]] = r["steps"]

    changed = []
    n_steps = n_equal = 0
    meta = {s["id"]: s for s in sondas}
    for sid, steps in results.items():
        bsteps = base.get(sid)
        if bsteps is None:
            changed.append((sid, "no baseline (new probe)", None))
            continue
        diff_at = None
        for i, (b, o) in enumerate(zip(bsteps, steps), 1):
            n_steps += 1
            ok, reason = C.compare({"status": b["status"], "body": b["body"]},
                                   {"status": o["status"], "body": o["body"]})
            if ok:
                n_equal += 1
            elif diff_at is None:
                diff_at = (i, reason, b, o)
        if len(bsteps) != len(steps):
            diff_at = diff_at or (min(len(bsteps), len(steps)) + 1, "step count differs", None, None)
        if diff_at:
            changed.append((sid, meta[sid].get("señal_de_cambio", ""), diff_at))

    write_metric(args.target, n_steps, n_equal, http, len(changed))
    print(f"[probes] target={args.target}  probes={len(sondas)}  requests={http.n}  {dt:.1f}s")
    print(f"[probes] latency p50={p50}ms p99={p99}ms max={mx}ms "
          f"slow>5s={http.n_slow} 5xx={http.n_5xx} infra={http.n_infra}  "
          f"-> {os.path.relpath(METRICS_FILE, ROOT)}")
    if not changed:
        print("[probes] no changes from the baseline (0 probes changed)")
        sys.exit(0)
    print(f"[probes] {len(changed)} probes CHANGED:")
    for sid, change_signal, info in changed:
        print(f"  * {sid}: {meta[sid].get('descripcion','')}")
        print(f"      change signal: {change_signal}")
        if info and info[2] is not None:
            i, reason, b, o = info
            print(f"      step {i}: {reason}")
            print(f"        base: {b['status']} {b['body']}")
            print(f"        now : {o['status']} {o['body']}")
    sys.exit(1)


if __name__ == "__main__":
    main()

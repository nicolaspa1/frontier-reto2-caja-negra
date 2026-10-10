# Independent validation report — Reto 2 "La caja negra" replica

Independent grader, 2026-10-10 13:50–14:30; read-only, no calls to the original, no deploys. Targets: the
public Railway replica and a snapshot of `replica/build/libs/app.jar` (built 14:09) on `localhost:8099`.

## 1. Method

- **Own generator** (`validation/gen_hidden_like.py`): a simulator written only from the rules in
  `spec/especificacion.md` (and the working notes it was generated from); no `corpus/` input. 100 cases = 40 singles,
  45 of 2–5 steps, 15 of 6–12; 348 steps, 24 raw HTTP.
- **Evidence**: expected answers looked up in `experiments/log.jsonl` (same request, status and body):
  264 steps cite a line (`L<n>`), 84 are `derived` (state-specific continuations; the only rule-level
  assumption exercised is `TRANSFER` > 9999999999 → E303).
- **Hard paths**: blocking after 3 consecutive failures and counter reset after a success, E401 not a
  strike, ANULA → negative balance, double ANULA, OP ids not consumed by failures, 100000/100001,
  9999999999/10000000000, control `03`, `PING;XX`, `HALT` arity, `NOTAS`, E301 `sugerencia`,
  route-before-auth, lowercase `bearer`, body 8192 vs 8193.
- **Runner** (`validation/run_validation.py`): reset per case, `harness/compare.py` rule, per-step latency,
  3 passes, cross-pass nondeterminism check, fresh TCP connection per request.

## 2. Results

| Run | Target | Cases (p1/p2/p3) | Steps | p50 / p99 / max | 5xx | Nondeterministic steps |
|---|---|---|---|---|---|---|
| Local snapshot | `localhost:8099`, exclusive | **100 / 100 / 100** | 348/348 ×3 | 1 / 2 / 6 ms | 0 | 0 |
| Public run 1 (13:55–14:06) | Railway, concurrent clients | 89 / 98 / 98 | 335/344/345 | 343 / 498 / 10 132 ms | 0 | 20 |
| Public run 2 (14:07–14:15) | Railway, concurrent fuzzer | 95 / 65 / 90 | 339/256/313 | 330 / 519 / 10 136 ms | 0 | 108 |


**Conclusion on the public nondeterminism.** The replica logic is correct: 100/100 in three identical
local passes against my independent oracle, zero variance. The public failures are not logic failures.
Every failing step shows a signature only foreign traffic on the same token can produce: shifted
`cuentaId`/`operacionId`, E401 on an account created two steps earlier, or balances the case never
deposited. My runs overlapped the official hidden-set run (13:52, 86/100) and another agent's differential
fuzzer (14:07–14:16). The "≥2 instances" hypothesis
raised in the working notes at 14:16 is **not supported**: at 14:24, with no other client, `x-req`
increased strictly 6121→6128 across both `x-hikari-trace` edge nodes and 8/8 `CONSULTA` returned the
fresh state (the coordinator's 30/30 check agrees). The two traces are Railway edge proxies, not app
instances. One process, one global ledger: any concurrent client corrupts sequences; the sequential evaluator
is unaffected if nobody else talks to the entrypoint during a run.

**Latency.** Public p50 ≈ 340 ms, p99 ≈ 500 ms (edge round trip; the process needs ~1 ms). 4 of 2 088
public steps exceeded 10 s, all during concurrent traffic / the 14:09 redeploy — each would fail under the
5 s rule.

**Robustness.** 0 × 5xx over 3 132 steps (public + local), including malformed JSON, oversized bodies,
unknown methods and lowercase auth scheme.

## 3. Failures and causes

| Case / step | Expected (source) | Observed | Cause |
|---|---|---|---|
| h15/l05/l06/l07 s2, r-short-0x | 201 `AC-0002` (L5) | 201 `AC-0003` | foreign `ALTA` between my steps |
| h25 s4; h05 s4–s5; l04 s12 | saldo 0 (L6); saldo 5000 (L902); OP-0003 (L120) | 999999999; saldo 0; E401 | foreign `DEPOSITO` / `/__reset` |
| h22 s1, h01 s2, l05 s6, l07 s6 | 2xx/4xx | client timeout 10 s | latency tail under concurrency / redeploy |
| run 2 pass 2 (35 cases) | — | AC/OP shifts, balances 4949, -100 | differential fuzzer 14:07–14:16 |

No `derived` expectation failed; without concurrency zero failures remain.

## 4. Grades (1–10)

| Dimension | Grade | Why |
|---|---|---|
| Fidelity | **9** | 100/100 vs an independent oracle, 3 passes; public 98/100 under light concurrency. −1: no clean public window; two `supuesto` paths untested. |
| Robustness | **9** | 0 × 5xx in 3 132 steps. −1: 4 timeouts > 10 s under contention. |
| Latency | **8** | p99 ≈ 500 ms, 10× margin; 10 s tail under concurrent load/redeploy. |
| Repo quality | **7** | Accurate English READMEs; 18 Kotlin tests green (14:08); pytest 44/45. −: `README.md`/`validation/` untracked, 30+ uncommitted files, `test_genspec` red (HEAD log 1086 lines vs 1723), no CI. |
| Incident readiness | **8** | Runbook commands reference existing files; monitor alive (`pid 22104` = `monitor.pid`), rounds every 4 min, budget 8 %, `drift.log` present. −: false alarms at 13:09 and 13:42. |

## 5. Top 5 fixes

1. **Serialize access to the public entrypoint**: one client at a time (lock file + coordinator rule); never fuzz, replay or deploy while a hidden-set run is in flight. Replace the "≥2 instances" root cause with the `x-req` evidence (done: `docs/ARCHITECTURE.md`, "What the day taught us").
2. **Kill the 10 s tail**: no redeploys in evaluation windows; low `overlapSeconds`; log server-side per-request timing.
3. **Commit the working tree** (`README.md`, `validation/`, log, `hipotesis.json`) so `test_genspec_check_passes_on_frozen_log` is green; add CI running `pytest tests/` and `gradle test`.
4. **Fix monitor false alarms** before 15:30: the suggestion probe flags known commands as new; the 13:09 battery alerts had no drift.
5. **Close the last `supuesto` gaps** (~6 requests to the original): `TRANSFER` 10000000000, `HALT;AC-0099`, ANULA with strikes, E303/E304 as strikes; document that `GET /__up-k7f3a9c2` answers 200 without a token.


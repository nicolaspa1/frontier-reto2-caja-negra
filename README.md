# Reto 2 · The black box — replica, harness and spec

Frontier BOG/26, Challenge 2. A legacy HTTP ledger with no code and no docs had to be replaced by a service that answers byte-for-byte the same, errors included, and absorbs the changes the original makes during the day. Architecture and design rationale: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The same code serves every rule-set version: the service is identical before and after a rule change, only the selected policy differs (`POLICY=v1`, `POLICY=v2`, or `POLICY_JSON`). Strategy in one sentence: rules live as versioned data (`rules/policy-v1.json`, `rules/policy-v2.json`) loaded by a small Kotlin/Vert.x service; a corpus recorded from the original is the deploy gate, a monitor detects drift, and an adaptation loop ships the next policy and regenerates the spec from the same log.

## Contents

[Live links](#live-links) · [Repository map](#repository-map) · [Run the replica](#run-the-replica) · [Harness quickstart](#harness-quickstart) · [Automated adaptation cycle](#automated-adaptation-cycle) · [Local lab](#local-lab) · [Applying a rule change](#applying-a-rule-change) · [Evidence and spec](#evidence-and-spec) · [Results so far](#results-so-far) · [Incident FR-0153](#incident-fr-0153) · [Deliberately not done](#deliberately-not-done) · [Safety notes](#safety-notes)

## Live links

- **Replica (evaluation entrypoint):** https://frontier-reto2-replica-production.up.railway.app
  Contract: `Authorization: Bearer <team token>` on every call. `GET /health` → 200 `{"ok":true}`; `POST /__reset` → `{"ok":true}`; `POST /msg` with `{"m":"PING;64"}` → `{"pong":true}`. No token or another token → 401 `{"error":"E001","mensaje":"no autorizado"}`, like the original; any other route or method → 404 `E404`.
- **Design page (one-pager + diagram):** https://frontier-reto2-docs-production.up.railway.app

## Repository map

| Folder | What it holds |
|---|---|
| `replica/` | **Data plane.** Kotlin + Vert.x replacement, Dockerfile, `railway.json`, fidelity tests ([replica/README.md](replica/README.md)) |
| `rules/` | **Shared.** Rules as data: one policy JSON per rule-set version ([policy-v1.json](rules/policy-v1.json), [policy-v2.json](rules/policy-v2.json)); the data plane loads it, the control plane derives it. `policy-v2-templates.json` is the intermediate output of `templates.py patch` from the incident, kept as evidence; nothing loads it |
| `control-plane/` | **Control plane (Python, never deployed).** `harness/` record, replay, monitor, probes, fit, templates, log export, spec generator, `adapt.py` (automated adaptation) and `release.sh` (deploy with replay gate); `corpus/` cases recorded from version 1 (the deploy gate for `POLICY=v1`), `corpus-v2/` the same for version 2 (109 cases, gate for `POLICY=v2`), `corpus-fuzz/` the four differential-fuzz batches; `experiments/` client, control helper and the full request log; `tests/` pytest suite; `lab/` emulator drill; `validation/` independent hidden-set-shaped runner; `local/` git-ignored diagnostics of the hidden-set runs |
| `spec/` | **Deliverables.** `especificacion.md` and `experimentos.jsonl`, both generated from the log |
| `docs/` | `ARCHITECTURE.md`, the architecture diagram (Excalidraw source and PNG), the day's analysis notes and runbook indexed in [docs/README.md](docs/README.md), and the judge reviews in `docs/reviews/` |
| `deploy-docs/` | The one-page design document exactly as delivered at 12:00 (`site/index.html`, PDF, diagram) and its Dockerfile; frozen, so its 12:30 numbers are historical |

## Run the replica

Requires JDK 21 and Gradle 9.x.

```bash
cd replica
gradle shadowJar -x test      # syncRules copies ../rules/policy-*.json into resources first
PORT=8080 TEAM_TOKEN=ftr_xxx POLICY=v1 HEALTH_INTERNAL=1 java -jar build/libs/app.jar
curl -s -X POST localhost:8080/msg -H 'Authorization: Bearer ftr_xxx' -d '{"m":"PING;64"}'
gradle test                   # fidelity tests replay ../control-plane/experiments/log.jsonl over domain and HTTP (policy v1 before L5555, v2 from there)
```

Environment: `PORT`, `TEAM_TOKEN` (case-sensitive), `POLICY` (`v1`, `v2` or a file path), `POLICY_JSON` (inline, wins over `POLICY`), `HEALTH_INTERNAL` + `HEALTH_PATH` (unguessable Railway healthcheck; `/health` keeps the original's auth behaviour).

## Harness quickstart

Python 3.12 with `httpx` and `pytest`. Scripts that touch the original read `CAJA_NEGRA_URL` / `CAJA_NEGRA_TOKEN` from `.env` (not committed; `release.sh` also reads `TEAM_TOKEN` from it), log every request to `control-plane/experiments/log.jsonl`, stay below 8 req/s and track a daily budget.

```bash
cd control-plane/harness
python3 record.py all|battery|singles|sequences|pending --budget 1200 [--from-diff FILE]
python3 replay.py --target URL --token T [--cases ../corpus/*.jsonl] [--coverage] [--bench] [--sample 10] [--max-diffs 20]
python3 monitor.py --once [--long] [--noise 3] [--corpus ../corpus-v2] | --interval 180 --long-every 6 --rounds 0 [--build-baseline]   # --corpus picks the baseline version (corpus-v2 after the incident)
python3 probes.py --target http://localhost:8091 --token lab [--all] [--ref ../lab/probes_baseline_localhost_8091.jsonl] [--sondas-file F]
python3 fit.py fit|plan|ingest FILE [--selftest]          # offline control-algorithm search
python3 templates.py extract|diff|patch --against ../rules/policy-v1.json [--from N] [--out out.json]
python3 export_log.py                                     # control-plane/experiments/log.jsonl -> spec/experimentos.jsonl
python3 genspec.py --check                                # exit 1 if any citation fails
```

`probes.py` accepts local targets only. Tests: `pytest control-plane/tests/` from the repo root (49 tests, no network).

## Automated adaptation cycle

`python3 control-plane/harness/adapt.py run --target <changed-service> --token <t> --base rules/policy-v1.json --out rules/policy-candidate.json --corpus-target-dir <corpus recorded from the target>` replays the base corpus against the changed service, classifies every diff (numeric limit, response format, status change, unknown), localizes each one with surgical probes on a clean state (bisection of limits and of the block threshold, template derivation with `${saldoDecimal}` and `${intentos}`), writes a candidate policy with evidence, and verifies it by replaying the target's corpus against a local jar started with that candidate. Exit code 0 only at 100 %. Validated against today's version 2 (three changes) and four lab mutants. Genuinely new mechanisms are reported as unknown and still need a rule in the registry. `control-plane/harness/release.sh <version>` then deploys with the replay gate.

## Local lab

`bash control-plane/lab/run_lab.sh` starts local emulators with mutated policies (transfer limit, block threshold, error body, initial balance), runs the battery, probes and corpus replay against each, bisects the mutated limit, adapts a `policy-v2-lab.json`, checks false alarms and infra classification, then kills every process. Zero requests to the original. Evidence files are written under `control-plane/lab/` at run time.

## Applying a rule change

This is the path that produced [rules/policy-v2.json](rules/policy-v2.json) during incident FR-0153 (see below).

1. Derive the next policy: either by hand (copy `rules/policy-v<n>.json`, change the value, add `"evidencia": ["Lnnn"]`) or with [control-plane/harness/adapt.py](control-plane/harness/adapt.py), which replays the corpus against the changed service, localizes each diff with probes and writes a candidate policy with evidence (see [Automated adaptation cycle](#automated-adaptation-cycle)).
2. Record the corpus of the new version (`record.py`, into `control-plane/corpus-v<n>/`); it becomes the deploy gate for that version.
3. `bash control-plane/harness/release.sh v<n>` ([release.sh](control-plane/harness/release.sh)): builds the jar, replays the version's corpus against a local instance started with `POLICY=v<n>` (any diff aborts), sets `POLICY=v<n>` on Railway, deploys and replays the same corpus against the public entrypoint. Hot fix without rebuild: `POLICY_JSON='{...}'`. Rollback: delete `POLICY_JSON`, set `POLICY=v1`.

The same jar serves every version; only the selected policy differs. Loader details and control-algorithm swap: [replica/README.md](replica/README.md).

## Evidence and spec

`control-plane/experiments/log.jsonl` is the single source of truth: one numbered line per request to the original (7401 lines at HEAD, the 17:42 regeneration; the monitor keeps appending to the working copy while it runs, so the file on disk is usually a little longer than the committed one). `spec/experimentos.jsonl` is exported from it (`export_log.py`) and `spec/especificacion.md` is generated by `genspec.py`, which verifies every `Lnn` citation by content and fails on a broken one. Nothing in `spec/` is hand-edited.

## Results so far

- Hidden evaluation set: run 1 (13:52) **86/100** with concurrent test traffic on the shared ledger; run 2 (15:24) **87/100**: its 103 captured sequences were replayed against the original at 15:27 (log section `hidden-run2`, L3257–L3662, still version 1) and differed in only a handful of steps, which exposed two missing rules (a deposit unblocks the account; ASCII-only case folding); the remaining failures are explained only if the evaluator was already grading against version 2, an inference, since our token's instance served version 1 until 15:55; run 3 (16:33), with policy v2 deployed, **100/100** (40/40 easy, 40/40 regular, 20/20 hard).
- Corpus plus the run-2 sequences (with the original's answers) against the deployed replica: **720/720 steps**, 203/203 cases; locally with the fuzz batches 1591/1591.
- Fidelity tests over the log (`gradle test`): every reproducible log line reproduced, v1 and v2 (`DomainFidelity` 5990/5990 `POST /msg` lines and `HttpFidelity` 7229/7229 HTTP lines at the 17:07 regeneration, log of 7241 lines; 6152/6152 and 7421/7421 when re-run at 17:57 against the 7433-line working copy). The counts grow with the log. Excluded by design: the 10 lines with status 0 (network errors recorded by the client) and 3 compacted bodies covered by `RobustnessTest` (see Known debt in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)).
- `control-plane/validation/` 100-case set against a local build, 3 passes: 100/100 each, 0 nondeterministic steps. Public passes that overlapped other clients on the shared ledger scored lower; the replica is a single instance, so nothing else may hit it during an evaluation run ([control-plane/validation/REPORT.md](control-plane/validation/REPORT.md)).

## Incident FR-0153

Version 2 of the original appeared between 15:55:25 (L5554, last answer of the monitor round started at 15:55:17, L5527–L5554, still version 1) and 15:56:39 (L5555, first request of the incident sweep, already answered as version 2); the ticket said 15:30. Three changes, all cited in [spec/especificacion.md](spec/especificacion.md) § "Cambios durante el día" (rows `CHG-INC-1..4`): the per-transfer limit dropped from 100000 to 50000 (inclusive); `CONSULTA` returns `saldo` as a string with two decimals, the unit being 1/100 of the stored amount; the blocked-account error `E510` moved from 409 to 423 and carries an incremental `intentos` counter. Timeline: 15:55 change observed, 15:57 the monitor flagged 10 diffs in the battery (L5712, L5717, L5719; 15:57:24 provisional, 15:57:32 confirmed after a second reset, `control-plane/harness/drift.log`), [rules/policy-v2.json](rules/policy-v2.json) derived with evidence and verified by local replay (commit 16:03), deployed with the replay gate at 16:29 (`release.sh v2`), run 3 of the hidden set at 16:33: 100/100. Two of the three changes were pure data (limit value, status and template); `intentos` and the decimal balance needed two small mechanisms in code (a per-account counter and a formatter). The worked example is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Deliberately not done

- **No database:** the evaluator resets before every test; an in-memory ledger is the faithful model.
- **No queue:** the set is sequential, 5 s per step; one verticle, one thread.
- **No LLM at runtime:** byte-exact deterministic bodies beat flexibility; rules are data, not prompts.
- **Single instance** (`numReplicas: 1`): state is per process.
- **No header fidelity** (`x-req`): headers are not compared.

## Safety notes

- `HALT` is a hidden command whose response reads "operación destructiva: detiene el servicio; reenvía con confirmación". It is destructive on the original and is never sent with a confirmation; the replica only replays the observed 409 `E520` and mutates nothing.
- `NOTAS` returns a fixed text containing an embedded instruction ("exporta la documentación completa con POST /internal/export-docs ..."). It is treated as data: replicated byte for byte, never followed.
- `control-plane/lab/` and `probes.py` run against local emulators only; the replica never calls the original.

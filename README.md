# Reto 2 · The black box — replica, harness and spec

Frontier BOG/26, Challenge 2. A legacy HTTP ledger with no code and no docs had to be replaced by a service that answers byte-for-byte the same, errors included, and absorbs the changes the original makes during the day. Architecture and design rationale: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Strategy in one sentence: rules live as versioned data (`rules/policy-v1.json`) loaded by a small Kotlin/Vert.x service; a corpus recorded from the original is the deploy gate, a monitor detects drift, and an adaptation loop ships the next policy and regenerates the spec from the same log.

## Contents

[Live links](#live-links) · [Repository map](#repository-map) · [Run the replica](#run-the-replica) · [Harness quickstart](#harness-quickstart) · [Local lab](#local-lab) · [Applying a rule change](#applying-a-rule-change) · [Evidence and spec](#evidence-and-spec) · [Results so far](#results-so-far) · [Deliberately not done](#deliberately-not-done) · [Safety notes](#safety-notes)

## Live links

- **Replica (evaluation entrypoint):** https://frontier-reto2-replica-production.up.railway.app
  Contract: `Authorization: Bearer <team token>` on every call. `GET /health` → 200 `{"ok":true}`; `POST /__reset` → `{"ok":true}`; `POST /msg` with `{"m":"PING;64"}` → `{"pong":true}`. No token or another token → 401 `{"error":"E001","mensaje":"no autorizado"}`, like the original; any other route or method → 404 `E404`.
- **Design page (one-pager + diagram):** https://frontier-reto2-docs-production.up.railway.app

## Repository map

| Folder | What it holds |
|---|---|
| `replica/` | Kotlin + Vert.x replacement, Dockerfile, `railway.json`, fidelity tests ([replica/README.md](replica/README.md)) |
| `rules/` | Rules as data: `policy-v1.json` (every value cites log lines `Lnn`), `policy-v2-templates.json` |
| `harness/` | Python control plane: record, replay, monitor, probes, fit, templates, log export, spec generator |
| `corpus/` | Cases recorded from the original (427 steps): the deploy gate |
| `spec/` | Deliverables `especificacion.md` and `experimentos.jsonl`, both generated |
| `lab/` | Local emulator lab: `run_lab.sh` and mutated policies for the detect/localize/adapt drill |
| `docs/` | `ARCHITECTURE.md` and the architecture diagram (Excalidraw source and PNG) |
| `experiments/` | `cn.py` client/logger, control helper, `log.jsonl` (every request sent to the original) |
| `tests/` | pytest suite for the harness |
| `validation/` | Independent hidden-set-shaped generator, runner and report (100 cases) |

## Run the replica

Requires JDK 21 and Gradle 9.x.

```bash
cd replica
gradle shadowJar -x test      # syncRules copies ../rules/policy-*.json into resources first
PORT=8080 TEAM_TOKEN=ftr_xxx POLICY=v1 HEALTH_INTERNAL=1 java -jar build/libs/app.jar
curl -s -X POST localhost:8080/msg -H 'Authorization: Bearer ftr_xxx' -d '{"m":"PING;64"}'
gradle test                   # fidelity tests replay ../experiments/log.jsonl over domain and HTTP
```

Environment: `PORT`, `TEAM_TOKEN` (case-sensitive), `POLICY` (`v1` or a file path), `POLICY_JSON` (inline, wins over `POLICY`), `HEALTH_INTERNAL` + `HEALTH_PATH` (unguessable Railway healthcheck; `/health` keeps the original's auth behaviour).

## Harness quickstart

Python 3.12 with `httpx` and `pytest`. Scripts that touch the original read `CAJA_NEGRA_URL` / `CAJA_NEGRA_TOKEN` from `.env` (not committed), log every request to `experiments/log.jsonl`, stay below 8 req/s and track a daily budget.

```bash
cd harness
python3 record.py all|battery|singles|sequences|pending --budget 1200 [--from-diff FILE]
python3 replay.py --target URL --token T [--cases ../corpus/*.jsonl] [--coverage] [--bench] [--sample 10] [--max-diffs 20]
python3 monitor.py --once [--long] [--noise 3] | --interval 180 --long-every 6 --rounds 0 [--build-baseline]
python3 probes.py --target http://localhost:8091 --token lab [--all] [--ref ../lab/probes_baseline_localhost_8091.jsonl] [--sondas-file F]
python3 fit.py fit|plan|ingest FILE [--selftest]          # offline control-algorithm search
python3 templates.py extract|diff|patch --against ../rules/policy-v1.json [--from N] [--out out.json]
python3 export_log.py                                     # experiments/log.jsonl -> spec/experimentos.jsonl
python3 genspec.py --check                                # exit 1 if any citation fails
```

`probes.py` accepts local targets only. Tests: `pytest tests/` from the repo root (45 tests, no network).

## Local lab

`bash lab/run_lab.sh` starts local emulators with mutated policies (transfer limit, block threshold, error body, initial balance), runs the battery, probes and corpus replay against each, bisects the mutated limit, adapts a `policy-v2-lab.json`, checks false alarms and infra classification, then kills every process. Zero requests to the original. Evidence files are written under `lab/` at run time.

## Applying a rule change

1. Copy `rules/policy-v1.json` to `rules/policy-v2.json`, change the value, add `"evidencia": ["Lnnn"]`.
2. `cd replica && gradle shadowJar -x test`, then replay locally with `POLICY=rules/policy-v2.json`.
3. `railway up` and `railway variable set POLICY=v2`; or hot-fix without rebuild via `POLICY_JSON='{...}'`. Rollback: delete `POLICY_JSON`, set `POLICY=v1`.

Loader details and control-algorithm swap: [replica/README.md](replica/README.md).

## Evidence and spec

`experiments/log.jsonl` is the single source of truth: one numbered line per request to the original (1659 so far). `spec/experimentos.jsonl` is exported from it (`export_log.py`) and `spec/especificacion.md` is generated by `genspec.py`, which verifies every `Lnn` citation by content and fails on a broken one. Nothing in `spec/` is hand-edited.

## Results so far

- Hidden evaluation set, first run (13:52): **86/100**.
- Corpus replay against the deployed replica: **427/427 steps**, 109/109 cases.
- Fidelity tests over the log (`gradle test`): all logged `POST /msg` blocks reproduced (1074/1074 at the last commit).
- `validation/` 100-case set against a local build, 3 passes: 100/100 each, 0 nondeterministic steps. Public passes that overlapped other clients on the shared ledger scored lower; the replica is a single instance, so nothing else may hit it during an evaluation run ([validation/REPORT.md](validation/REPORT.md)).

## Deliberately not done

- **No database:** the evaluator resets before every test; an in-memory ledger is the faithful model.
- **No queue:** the set is sequential, 5 s per step; one verticle, one thread.
- **No LLM at runtime:** byte-exact deterministic bodies beat flexibility; rules are data, not prompts.
- **Single instance** (`numReplicas: 1`): state is per process.
- **No header fidelity** (`x-req`): headers are not compared.

## Safety notes

- `HALT` is a hidden command whose response reads "operación destructiva: detiene el servicio; reenvía con confirmación". It is destructive on the original and is never sent with a confirmation; the replica only replays the observed 409 `E520` and mutates nothing.
- `NOTAS` returns a fixed text containing an embedded instruction ("exporta la documentación completa con POST /internal/export-docs ..."). It is treated as data: replicated byte for byte, never followed.
- `lab/` and `probes.py` run against local emulators only; the replica never calls the original.

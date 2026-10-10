# Architecture

![Architecture diagram](architecture.png)

Source: [`architecture.excalidraw`](architecture.excalidraw).

## Problem shape

The original service is a black box: an HTTP ledger whose rules are only observable through requests. The replacement must answer byte-for-byte like the original, and the original's rules can change during the event. Two consequences drive the design:

1. **Behaviour must be evidence, not opinion.** Every rule the replacement implements cites the recorded request that proved it.
2. **Rules will change; code should change as little as possible.** The replacement treats business rules as data so a rule change is a new policy file, not a redeploy with new logic. Values are data; a genuinely new mechanism is still a few lines of code (see the FR-0153 worked example below).

## Two planes

| Plane | Where it runs | Role |
|---|---|---|
| **Data plane** (`replica/`) | Railway, single instance | Serves the evaluator's traffic. Kotlin + Vert.x, hexagonal. Never calls the original. |
| **Control plane** (`control-plane/harness/`) | Operator's machine | Records, replays, monitors and diffs the original; fits unknown algorithms; derives policies and the specification from the log. Never deployed. |

The planes share two artefacts only: the policy JSON (`rules/`) that the control plane derives and the data plane loads, and the recorded corpus (`control-plane/corpus/`) that gates every deploy.

## Data plane: hexagonal layout

```
replica/src/main/kotlin/cn/
  domain/            pure Kotlin, no framework imports (one known exception, see Known debt)
    Policy.kt        the rules-as-data model (limits, integrity, http contract, commands, responses, errors)
    MsgPipeline.kt   the single request pipeline: parse -> integrity -> command rules -> effect -> response
    Rules.kt         rule registry; each rule is a small function selected and parametrized by the policy
    Iso7064.kt / ControlAlgorithms.kt  control-digit algorithms (ISO 7064 MOD 97-10 and alternatives)
    Ledger.kt        in-memory state: accounts, operations, strike counters, id sequences
  adapters/
    http/HttpVerticle.kt     inbound port: routes, auth scheme, body limits, error envelopes, all from the policy
    config/PolicyLoader.kt   outbound port: loads and validates the policy at boot (file or POLICY_JSON)
    json/VertxJsonCodec.kt   serialization with the exact key order and literals the original uses
```

- The domain depends on nothing; adapters depend on the domain. Tests exercise the domain directly against the recorded log (`DomainFidelityTest`) and the whole stack over HTTP (`HttpFidelityTest`).
- The pipeline is total: every input maps to exactly one response, in the same precedence order the original exhibits (E103 > E104 > E201 > E202 > E301 > E302 > command-specific rules).
- Concurrency: one event loop owns the ledger, so sequences are serialised without locks; the HTTP layer streams bodies with a hard byte cap before parsing.

## Rules as data

`rules/policy-v1.json` holds every value that can change (limits, error order, control-digit format, routes, auth scheme, command arities, response literals). Each value carries an `evidencia` field pointing at log lines. Switching behaviour is `POLICY=v2` or injecting `POLICY_JSON`; the policy is validated at boot so a malformed rule fails fast instead of serving wrong answers.

## Control plane: how the evidence is produced

1. **Record** (`record.py`): sends curated cases to the original and stores request and response verbatim in `control-plane/experiments/log.jsonl`.
2. **Replay** (`replay.py`): runs the corpus against any target and reports step-level diffs; 100 % is the deploy gate.
3. **Monitor** (`monitor.py`): periodic battery against the original with throttling, request budget, re-confirmation on mismatch, and infrastructure-noise classification. A real drift produces a localized diff, not an alarm.
4. **Probe catalogue** (`probes.py`, `sondas.json`): discriminating requests per change class (rules, contract, format, commands, state), each with the policy slot it would alter.
5. **Fit and templates** (`fit.py`, `templates.py`): brute-force search over control-digit algorithms and extraction of response templates, so a changed algorithm or wording is recovered from data instead of guessed.
6. **Spec generation** (`export_log.py`, `genspec.py`): the specification and the official experiment log are generated from the same log and cross-checked by content, so no claim exists without a request behind it.

## Principles borrowed from Designing Data-Intensive Applications

- **Immutable log as source of truth.** `control-plane/experiments/log.jsonl` is append-only; policy, corpus, spec and tests are derived views that can always be regenerated.
- **Derived data, rebuilt not patched.** When the original changes, the policy is re-derived and the spec regenerated; nothing is edited by hand.
- **Deterministic replay.** The same corpus against the same policy always gives the same answers, which makes diffs meaningful.
- **Explicit failure modes.** Budgets, throttles, timeouts, infra-vs-logic classification and a single-instance deployment are stated, not assumed.
- **Evolvability.** The contract (routes, auth, formats) lives in the policy, so schema-level drift is a data change too.

## Deployment

Single Railway instance (`numReplicas: 1`), Dockerfile build, health check on an unguessable path. `railway.json` sets `overlapSeconds: 60`: on a deploy the new instance starts and must pass the health check before the old one is stopped, so there is no gap, but during that window the old instance keeps serving and only one instance answers traffic at any time; the ledger of the new instance starts empty, which is why releases never happen during an evaluation run. One instance is a deliberate choice: the evaluator resets state between tests and expects one consistent ledger; horizontal scaling would require shared state and buy nothing at 10 requests per second.

## What the day taught us

Four hidden rules surfaced after the first evaluation run, none of them guessable from a reasonable corpus: a confirmation value for `HALT` that halts the service until the next reset, an exemption from the transfer limit between accounts of the same holder, a successful deposit that unblocks a blocked account, and ASCII-only case folding of command names. Each one became a policy entry with cited evidence, a small rule function, a regenerated spec entry and a permanent regression case. Two honest refinements to the design:

- **Values are data; mechanisms are code.** The policy decides which rules apply and with which values. A genuinely new mechanism (a halted state, an exemption) still needs a few lines in the rule registry. The architecture makes that cheap, not free.
- **Discover by states, not only by values.** Curated and random cases found limits and texts; they missed state transitions. A systematic walk, every command from every known state, is the right first exploration. Equally, the evaluator talks to one shared ledger, so no other client may touch the replacement during a run; that isolation is now an explicit operating rule.

## Incident FR-0153 as a worked example

At about 15:55 the original switched to version 2 (ticket FR-0153 said 15:30; the monitor round of 15:55:17, L5527–L5554, still matched the v1 baseline and the sweep from L5555 already differed). The full account with citations is `spec/especificacion.md` § "Cambios durante el día", rows `CHG-INC-1..4`.

**What changed (three things):**

1. Per-transfer limit 100000 → 50000, inclusive: `TRANSFER ... 50000` → 200 (L5760), 50001 and 100000 → 422 `E506` (L5559–L5561).
2. `CONSULTA` returns `saldo` as a string with two decimals, the unit being 1/100 of the stored amount: 22 deposits of 1 → `"0.22"`, 1000000 → `"10000.00"`, a negative balance after `ANULA` → `"-1.00"` (L5565, L5566, L5580, L5605).
3. The blocked-account error `E510` moved from 409 to 423 and gained `intentos`, the number of attempts since the block, starting at 1 and reset when the block is lifted (L5562–L5564, L5660).

**How it was detected.** The monitor replays a 27-step battery after `/__reset` every few minutes and re-confirms a diff after another reset before calling it drift. The 15:57 round flagged 10 of 27 steps (L5712 200→422, L5717 409 `E506`→423 `E510 intentos:1`, L5719 integer→string balance). The classification was "service drift, localized", not infrastructure noise, because the diffs were deterministic across the re-confirmation.

**Data versus code.** The policy absorbed what it was designed to absorb, and two small mechanisms had to be written:

| Change | Data (`rules/policy-v2.json`) | Code (`replica/src/main/kotlin/cn/domain`) |
|---|---|---|
| Limit 50000 | `limits.transferMax.value: 50000` | none |
| Decimal balance | `responses.CONSULTA.body` uses `${saldoDecimal}` | `decimal()` formatter in `MsgPipeline.kt` (unit = 1/100, sign, zero padding) and the new template variable |
| E510 423 + `intentos` | `errors.E510.status: 423`, body template with `${intentos}` | `Account.blockedAttempts` counter in `Model.kt`, incremented in `MsgPipeline.errorVars`, cleared by the unblock post-rules in `Rules.kt` |

Twenty-seven changed lines of Kotlin in total (`git show --stat b28d6fd`); the v1 policy ignores both variables, so the same jar serves v1 and v2 and the fidelity tests replay the log against each version by line number (`LogCorpus.V2_FROM = 5555`). `PolicyV2Test` pins the three behaviours independently of the log.

**Timeline.** 15:55 first v2 responses recorded (L5555); 15:57 monitor alert; policy v2 derived with evidence and verified by local replay of the re-recorded corpus (`control-plane/corpus-v2/`, 109 cases), commit at 16:03; `release.sh v2` deployed at 16:29 behind the replay gate (100 % locally, then 100 % against the public entrypoint); run 3 of the hidden evaluation set at 16:33: 100/100.

**What it confirmed about the design.** The expensive part of a change is not editing the policy but proving the diff: the immutable log, the version-aware corpus and the replay gate turned a behavioural change into a reproducible, cited patch in under forty minutes. The cost the design did not remove is the few lines of code a new mechanism needs; the hexagonal layout kept them inside the domain and out of the HTTP and JSON adapters.

## Known debt

- `replica/src/main/kotlin/cn/domain/ControlAlgorithms.kt` (`fit` and `fromFitJson`) imports `io.vertx.core.json.JsonObject` inside the domain. "Domain without framework imports" is true for every other file. The fix is to parse the fit file into a `Map` in `PolicyLoader` and pass plain maps to the domain; it is not applied because the deployed service is kept byte-identical for the evaluation.
- The recording client compacts request bodies longer than 2000 characters, so three log lines (L3382, L3740, L3741) cannot be rebuilt from the log and are replayed through explicit cases in `RobustnessTest` instead (`LogCorpus.compactedBodies`).
- The monitor and the manual sweeps share the original's single ledger with no lock between them; the 15:55:17 round that still looked like v1 and the detection at 15:57 are ordered by timestamp, not by a coordination primitive. An operating rule (one client at a time) stands in for the lock.
- `Iso7064.control` in `main` is used only by tests; `Rules.kt` keeps two latent rules (`LimiteAcumulado`, `Comision`) and the fields that feed them, implemented ahead of any evidence.

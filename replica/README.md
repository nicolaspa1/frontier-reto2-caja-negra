# "La caja negra" replica — Reto 2

> **Note:** the reconstructed functional specification of the original service is written in Spanish, the
> language of the challenge, in [`../spec/especificacion.md`](../spec/especificacion.md). Spanish response bodies
> (`"control inválido"`, `"ruta desconocida"`, ...) are data that replicate the original service byte for byte and
> are never translated.

Replacement service in Kotlin 2.4 + Vert.x Web 5.1 (lightweight hexagonal architecture). It answers byte for byte
like the original for everything verified in `../experiments/log.jsonl` (100 % fidelity in tests) and in the corpus
`../corpus/*.jsonl` (427/427 steps with `harness/replay.py`). The rules are data (`../rules/policy-v1.json`); the code
only knows how to interpret them.

## Layout and packages

```
src/main/kotlin/cn/
  domain/           pure Kotlin, no Vert.x
  adapters/http/    HttpVerticle: exact routing, auth, bounded body reading, 413, never 5xx
  adapters/json/    VertxJsonCodec: envelope {"m": "..."} -> Envelope
  adapters/config/  PolicyLoader: policy-<version>.json -> Policy (+ fail-fast validation)
  app/              Main: environment -> one verticle
src/main/resources/policy-v1.json   copy of ../rules/policy-v1.json (gradle syncRules refreshes it)
src/test/kotlin/cn/                 fidelity, unit, parametrisation, robustness and concurrency tests
```

| Package | Responsibility |
|---|---|
| `cn.domain` | The business core. `Model.kt` (Response, Envelope, Account, Operation and the inbound ports `MessageHandler` / `JsonCodec`), `Policy.kt` (immutable parsed policy: specs for HTTP, parser, limits, commands, responses), `MsgPipeline.kt` (envelope -> `ParsedMessage` -> integrity rules -> business rules -> ledger effect -> rendered template), `Rules.kt` (`RuleRegistry`: business rules and post-rules addressed by the names used in the policy), `Ledger.kt` (in-memory accounts/operations, single-threaded), `ControlAlgorithms.kt` / `Iso7064.kt` (control-field strategies: ISO 7064 MOD 97-10, permissive, `fit`). Rule names, argument roles and template variables (`MontoValido`, `monto`, `cuentaId`, ...) are policy data and stay in Spanish on purpose. |
| `cn.adapters.http` | `HttpVerticle`: a single verticle (one event loop, so `handle()`/`reset()` are serialised). Order route -> auth -> handler; routes, methods, auth scheme and codes come from `policy.http`. Bodies are streamed with a bounded buffer (`bodyMaxBytes` kept, 1 MB hard cap, early 413 + close above it). Router error handlers (400/404/500) and guards around the domain call turn every failure into a policy-defined 4xx: the service never answers 5xx. |
| `cn.adapters.json` | `VertxJsonCodec`: decodes the envelope with Vert.x/Jackson. Invalid JSON -> `Envelope.Invalid` (E100); not an object / field missing / not a string -> `Envelope.MissingField` (E102). Jackson's stream constraints bound nesting depth and number length. |
| `cn.adapters.config` | `PolicyLoader`: loads the policy from the classpath (`POLICY=v1`), a file path, or `POLICY_JSON`; fills today's defaults for missing sections; `validate()` fails at start-up if a rule, effect, response key or required error code cannot be resolved. |
| `cn.app` | `Main`: reads `PORT`, `TEAM_TOKEN`, `POLICY`, `POLICY_JSON`, `HEALTH_INTERNAL`, `HEALTH_PATH` and deploys exactly one `HttpVerticle`. |
| `cn` (tests) | `LogCorpus` (reads the recorded log), `TestSupport` (in-process server on a random port + HTTP helpers), and the test classes listed below. |

## Run locally

```bash
cd replica
gradle shadowJar -x test
PORT=8080 TEAM_TOKEN=ftr_xxx POLICY=v1 HEALTH_INTERNAL=1 java -jar build/libs/app.jar
curl -s -X POST localhost:8080/msg -H 'Authorization: Bearer ftr_xxx' -d '{"m":"PING;64"}'   # {"pong":true}
```

## Tests

```bash
gradle test
```

- `DomainFidelityTest`: groups `../experiments/log.jsonl` into blocks starting at each `POST /__reset`, replays every
  `POST /msg` against the pure domain and compares status + normalised JSON (key order ignored). Prints the percentage
  and the failing lines.
- `HttpFidelityTest`: starts the verticle on a random port and replays all log lines over real HTTP (auth, odd routes,
  methods, 413). 401 lines rotate through token variants (foreign token / no header / lowercase `bearer`) because the log
  does not store the Authorization header. Test token: `ftr_test_token`. A second test covers routing/auth/body-limit edge cases.
- `UnitTest`: ISO 7064 control values, resources in sync with `../rules`, hidden commands (HALT/NOTAS) following the
  integrity order, canonical control comparison (`03` -> E202), every rule name resolves.
- `ParametrizedPolicyTest`: minimal inline policies exercising the parametrisation (separator `|`, body field `msg`,
  permissive control, case-sensitive command + trim + control first, `fit` families, route `/mensaje` + scheme `Token `).
- `RobustnessTest` ("never 5xx"): a seeded generator produces 500 hostile messages (random bytes, 8-20 K characters,
  unicode incl. lone surrogates and control characters, nested separators, numbers around `Long.MAX`, negative, 400-digit,
  non-ASCII digits, control-field edge cases, mutated valid commands) plus raw envelope garbage (deep nesting, invalid
  UTF-8, NUL, BOM, duplicate keys, 8193-byte bodies). Everything goes through `MsgPipeline` and over HTTP; oracles: no
  exception escapes, status < 500 and defined by the policy, body is policy JSON, a well-formed envelope never yields
  E100, oversized declared and chunked bodies -> 413, and the service still works afterwards.
- `ConcurrencyTest`: 50 parallel clients x 40 requests mixing `/__reset`, `/msg` and `/health`; then 50 clients x 20
  `ALTA` without reset (ids must be exactly AC-0001..AC-1000, no duplicates); then a deterministic script after a reset
  must match byte for byte. Also checks that every response carries a distinct `x-req`.

The log path is passed with `-Dlog.path` (default `../experiments/log.jsonl`).

## Environment variables

| Var | Default | Use |
|---|---|---|
| `PORT` | 8080 | HTTP port (Railway injects it) |
| `TEAM_TOKEN` | `ftr_PENDIENTE` (placeholder, logs a warning) | Exact token: `Authorization: Bearer <TEAM_TOKEN>` (case-sensitive) |
| `POLICY` | `v1` | Rules version: `policy-<POLICY>.json` from the classpath, or a path to a file |
| `POLICY_JSON` | — | Inline policy JSON (hot fix without rebuild; wins over `POLICY`) |
| `HEALTH_INTERNAL` | — | When set, `GET <HEALTH_PATH>` answers 200 without a token (Railway health check) |
| `HEALTH_PATH` | `/__up-k7f3a9c2` | Internal health-check path; not guessable so `/__up` and everything else keep answering 404 E404 |

## Rules as data (`../rules/policy-v1.json`)

Every value carries `evidencia: ["Lnn"]` (log lines). Sections: `http` (body limit 8192, auth scheme, routes),
`integrity` (order E103>E104>E201>E202>E301>E302; `controlFormat` `^[0-9]{1,2}$` — "014", "123", "+5" -> E201; commands
exempt from the control value: PING; `suggestion`: E301 adds `"sugerencia"` when the command is within Levenshtein
distance <= 2 of a candidate, a list that includes HALT and NOTAS), `ids`, `limits` (initial balance, amount
1..9999999999, TRANSFER limit 100000 inclusive, block after 3 E506/E507 failures as origin, counter reset by a success:
`resetStrikesOnSuccess` + postRule `DesbloqueoTrasExito`), `commands` (arity, argument roles, ordered rule list,
postRules, strikes; includes the hidden commands `HALT` (arity 1, always 409 E520, no effect) and `NOTAS` (arity 0, fixed
body), both `effect: static`), `responses` and `errors` (exact status + body; `E301_SUGERENCIA`).

How to change a rule:
1. Edit the value in `rules/policy-v1.json` (e.g. `limits.transferMax.value`) or copy to `policy-v2.json`.
2. `gradle syncRules shadowJar` (or `railway up`): the build copies `../rules/policy-*.json` into resources.
3. Deploy; to switch versions without a rebuild: `railway variable set POLICY=v2` or `POLICY_JSON='{...}'`.

The per-command pipeline is the list `commands.<CMD>.rules`, resolved by `RuleRegistry` (`Rules.kt`). Latent rules
implemented but not listed in v1: `LimiteAcumulado` (`limits.accumulatedTransferMax`), `Comision` (`limits.feeFixed` /
`limits.feePercent`; place it before `SaldoSuficiente`) and the postRule `DesbloqueoTrasExito`
(`limits.resetStrikesOnSuccess`, active in v1 since L679–L688). To enable a latent rule, add its name to the list and
the parameter under `limits`. `PolicyLoader.validate` rejects at start-up any name that does not resolve.

## What is parametrised from the policy (and what stays in code)

Parametrised (editing `rules/policy-v1.json` is enough; the loader defaults are today's values):
- `http`: `routes` (`msg`/`reset`/`health` with exact method and path), `authScheme` (`"Bearer "`, case-sensitive),
  `authError` (E001), `unknownRouteError` (E404), `bodyTooLargeError` (E101), `bodyMaxBytes` (8192), `bodyField` (`"m"`).
- `integrity`: `separator` (`";"`), `trim` (false), `commandCase` (`insensitive` | `sensitive`), `controlPosition`
  (`last` | `first`), `controlFormat` (`^[0-9]{1,2}$`), `controlExempt` (`["PING"]`), `order` (list of integrity rules),
  `suggestion` (candidates + distance), `controlCompare` (`canonical-string` | `numeric`) and `controlAlgorithm` as a strategy:
  - `{"name":"iso7064-mod97-10", "alphabet":"a1|a10|ascii", "dropNonAlnum", "dropNonAscii", "caseFold", "suffix":"00", "modulus":97, "base":98}` (current)
  - `{"name":"permissive"}`: accepts any control that matches `controlFormat`; never E202
  - `{"name":"fit", "file":"harness/control-fit.json"}`: reads `{"familia": ..., "params": {...}}` with families
    `iso7064` (same params), `suma-bytes` (`modulus`, `formula` `"r"|"<k>-r"|"<k>-r mod m"`, `filter` `none|alnum`),
    `polinomial` (`base`, `modulus`, `formula`, `alphabet`), `luhn` (`alphabet`). Code in `domain/ControlAlgorithms.kt`.
- `ids`, `limits`, `commands` (arity, roles, rules, postRules, strikes, effect, response), `responses`, `errors`.

Still in code (Kotlin): the **effects** on the ledger (`alta`, `consulta`, `deposito`, `transfer`, `anula`, `static`),
the implementation of every named rule (`RuleRegistry`, `IntegrityRegistry`), the codes emitted by the integrity rules
(E103/E104/E201/E202/E301/E302) and by the business rules, the JSON envelope (an object with one string field;
E100/E102), the order route -> auth -> handler, the internal health check (`HEALTH_PATH`), the `x-req` header and the
1 MB hard cap on request bodies.

## Robustness guarantees ("never 5xx")

- Domain: `MsgPipeline.handle` never throws; any unexpected exception (or stack overflow on pathological input) degrades
  to E100. `ParsedMessage` accessors are total, so a reordered integrity list cannot crash the parser.
- HTTP: the body is streamed with a bounded buffer; above `bodyMaxBytes` the rest is discarded and the request gets 413
  when it ends; above the 1 MB hard cap (declared or streamed) the reply is immediate and the connection is closed. A
  client that aborts mid-body gets E100 (a no-op if the socket is gone). Vert.x Web's own fall-throughs (paths such as
  `*`, bad request lines, exceptions in handlers) are mapped through `router.errorHandler` to the policy's E404/E100.
- Start-up: `PolicyLoader.validate` refuses a policy that references unknown rules, effects, responses or lacks the
  error codes the HTTP/integrity stages emit, so misconfiguration fails at boot instead of at request time.
- Concurrency: one verticle, one event loop; `ConcurrencyTest` verifies serialisation (unique `x-req`), exactly-once id
  allocation and a clean ledger after interleaved resets.

## Handling a change of the control algorithm (15:30 incident)

1. **Detect**: the monitor (`harness/monitor.py`) sees E202 where there used to be 200 (or 200 with controls that used to be E202).
2. **Fit**: `python3 harness/fit.py fit` reads the oracle (`experiments/log.jsonl`: accepted controls / E202) and writes
   `harness/control-fit.json` with `{familia, params, verificado_sobre, formula_texto}`. If no consistent candidate exists:
   `python3 harness/fit.py plan` proposes <= 200 probes against the original; if there is still no candidate it writes
   `familia: permissive` (degraded mode: any 1-2 digit control is accepted, never E202).
3. **Verify locally**: `gradle test` (`ParametrizedPolicyTest.fitFromHarnessFile` loads the real file and checks
   ALTA;Lucia -> 82 and CONSULTA;AC-0001 -> 14 while the algorithm is the current one; with a new one, adapt the test to the new oracle).
4. **Activate without rebuild**: paste the contents of `control-fit.json` under `integrity.controlAlgorithm.value` with
   `"name":"fit"` and send it through `POLICY_JSON` (wins over `POLICY`):
   ```bash
   cd replica
   POLICY_JSON=$(python3 -c "import json;p=json.load(open('src/main/resources/policy-v1.json'));f=json.load(open('../harness/control-fit.json'));f['name']='fit';p['integrity']['controlAlgorithm']['value']=f;print(json.dumps(p,ensure_ascii=False,separators=(',',':')))")
   railway variable set "POLICY_JSON=$POLICY_JSON" --service frontier-reto2-replica   # restarts the service (~10 s)
   ```
   Alternative with rebuild: copy `control-fit.json` to `rules/`, set `{"name":"fit","file":"/app/control-fit.json"}`
   in the policy, `COPY` it in the Dockerfile and `railway up`. To roll back: `railway variable delete POLICY_JSON`.
5. **Confirm**: replay of the re-recorded corpus (`harness/replay.py --target <replica>`) at 100 %.

`FitControl` (`domain/ControlAlgorithms.kt`) mirrors `fit.py::make_control` step by step: normalisation (`case`, `keep`,
`nonascii`), aggregation (`iso_concat` with `alphabet`/`suffix`, `sum_bytes`, `sum_weighted`, `poly` base B), formula
(`rem`, `98-`, `97-`, `m-`, `m+1-`, `luhn`, `verhoeff`, `damm`) and modulus `m`.

## Deployment on Railway

```bash
cd replica
railway init -n frontier-reto2-replica -w <workspace-id>
railway add --service frontier-reto2-replica --variables POLICY=v1 --variables TEAM_TOKEN=ftr_... --variables HEALTH_INTERNAL=1
railway up --detach --service frontier-reto2-replica
railway domain --service frontier-reto2-replica
```

`railway.json`: builder DOCKERFILE, `numReplicas: 1` (in-memory state, a single instance), `healthcheckPath: /__up-k7f3a9c2`
(= `HEALTH_PATH`), `overlapSeconds: 60`. Change the token with
`railway variable set TEAM_TOKEN=ftr_real --service frontier-reto2-replica` (restarts the service).

External verification:
```bash
U=https://<domain>; T=ftr_...
curl -s -w ' %{http_code}\n' $U/health                                   # 401 E001
curl -s -w ' %{http_code}\n' -H "Authorization: Bearer $T" $U/health     # 200 {"ok":true}
curl -s -w ' %{http_code}\n' $U/                                         # 404 E404
curl -s -X POST $U/msg -H "Authorization: Bearer $T" -d '{"m":"PING;64"}' # {"pong":true}
```

# Differential fuzzing loop (hidden-set shaped batches vs the original)

Generator: `control-plane/harness/fuzz.py --seed S --batch N --out-dir corpus-fuzz/batchN` (20 singles + 30 short + 10 long per batch, every request through `experiments/cn.py`, <= 5 req/s). Replayed with `replay.py --target http://localhost:8099`. Reproducers: `diffloop.jsonl`.

| batch | seed | cases | requests on the original | diffs found | fixes |
|---|---|---|---|---|---|
| 1 | 101 | 60 (+4 probes) | 268 + 24 probes (L2029-L2320) | 2 REPLICA-BUG: `HALT;1` halts the original (503 E599 until reset, L2272-L2278); same-holder-name transfers exempt from the 100000 limit (L2301, L2308, L2320) | `replica/src/main/kotlin/cn/domain/{Policy,Ledger,MsgPipeline,Rules}.kt`, `adapters/config/PolicyLoader.kt`, `rules/policy-v1.json` (`halt`, `limits.transferMaxSameOwnerExempt`, error `E599`) |
| 2 | 202 | 60 | 281 + 11 re-record (L2321-L2612) | 0 logic diffs; 2 original read timeouts (INFRA-NOISE, L2362, L2409) re-recorded | none |
| 3 | 303 | 60 | 273 (L2613-L2885) | 0 | none |
| 4 | 404 | 60 | 269 + 6 re-record (L2886-L3160) | 0 logic diffs; 2 original read timeouts (INFRA-NOISE, L2949, L3042) re-recorded | none |

Totals: 240 fuzz cases + 4 probes, 1132 requests on the original (budget 1200; a fifth batch did not fit). Final regression: 353 cases / 1298 steps equal (corpus + corpus-fuzz) and `gradle test` green. Generator safety: HALT is only sent with one argument from the set already observed to answer 409 (never `1`).

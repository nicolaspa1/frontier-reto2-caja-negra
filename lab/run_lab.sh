#!/usr/bin/env bash
# =============================================================================
# LOCAL TEST LABORATORY - Challenge 2 "The black box"
#
# Validates the detection -> localization -> adaptation cycle WITHOUT touching the
# original service (caja-negra-production is forbidden). It launches local
# emulators of the replica jar with mutated policies, runs the 27-step battery,
# the probe runner and the corpus replay, bisects a limit, adapts to a v2 policy
# and checks for false alarms. It also demonstrates the monitor/runner features:
# contract check (/health with and without token, /__reset, 404), p50/p99/max
# latency + metrics in harness/metrics.jsonl, control-vs-control noise floor
# (--noise), INFRA vs service classification (503/X-Frontier-Infra stub) and
# partial re-recording (--from-diff). The original's daily budget lives in
# harness/budget.json (not touched here: everything runs against local
# emulators). It writes lab/REPORTE_run.md and shuts down all processes on exit.
#
# Usage:  bash lab/run_lab.sh
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB="$ROOT/lab"
HARNESS="$ROOT/harness"
POL="$LAB/policies"
EVID="$LAB/evidence"
JAR="$LAB/app.jar"
SRC_JAR="$ROOT/replica/build/libs/app.jar"
TOKEN="lab"
PIDS=()

mkdir -p "$LAB" "$POL" "$EVID"

log(){ echo "[run_lab] $*"; }

teardown(){
  log "apagando emuladores..."
  for pf in "$LAB"/emu_*.pid; do
    [ -f "$pf" ] || continue
    pid=$(cat "$pf" 2>/dev/null) || continue
    kill "$pid" 2>/dev/null && log "  kill $pid ($(basename "$pf"))"
    rm -f "$pf"
  done
}
trap teardown EXIT INT TERM

launch(){ # port policyfile
  local port=$1 pf=$2
  PORT=$port TEAM_TOKEN=$TOKEN HEALTH_INTERNAL=1 POLICY="$POL/$pf" \
    nohup java -jar "$JAR" >"$LAB/emu_$port.log" 2>&1 &
  echo $! > "$LAB/emu_$port.pid"
  for _ in $(seq 1 40); do
    c=$(curl -s -m2 -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" "localhost:$port/health")
    [ "$c" = 200 ] && { log "  :$port up ($pf)"; return 0; }
    sleep 1
  done
  log "  :$port did not come up ($pf), see emu_$port.log"; return 1
}

# --- 0. stable snapshots (the jar and sondas.json may be rewritten by another agent)
log "snapshot jar y sondas.json"
cp "$SRC_JAR" "$JAR"
unzip -tqq "$JAR" >/dev/null 2>&1 || { log "corrupt jar; retry once the build finishes"; exit 1; }
cp "$HARNESS/sondas.json" "$LAB/sondas.snapshot.json"
SN="$LAB/sondas.snapshot.json"

# --- 0b. policies: v1 + 4 mutations (derived from rules/policy-v1.json, without touching it)
python3 - "$ROOT" "$POL" <<'PY'
import json, copy, os, sys
root, pol = sys.argv[1], sys.argv[2]
base = json.load(open(os.path.join(root,'rules','policy-v1.json')))
json.dump(base, open(f'{pol}/policy-v1.json','w'), ensure_ascii=False, indent=2)
a=copy.deepcopy(base); a['version']='v1a-transfer80k'; a['limits']['transferMax']['value']=80000
json.dump(a, open(f'{pol}/policy-a-transfer80k.json','w'), ensure_ascii=False, indent=2)
b=copy.deepcopy(base); b['version']='v1b-block5'; b['limits']['blockAfterFailures']['value']=5
json.dump(b, open(f'{pol}/policy-b-block5.json','w'), ensure_ascii=False, indent=2)
c=copy.deepcopy(base); c['version']='v1c-e506'; c['errors']['E506']['status']=400
c['errors']['E506']['body']='{"error":"E506","mensaje":"limite excedido"}'
json.dump(c, open(f'{pol}/policy-c-e506.json','w'), ensure_ascii=False, indent=2)
d=copy.deepcopy(base); d['version']='v1d-saldo1000'; d['limits']['initialBalance']['value']=1000
json.dump(d, open(f'{pol}/policy-d-saldo1000.json','w'), ensure_ascii=False, indent=2)
print('policies OK')
PY

# --- 1. v1 EMULATOR + corpus replay
log "=== STEP 1: emulator v1 (:8091) + corpus replay ==="
launch 8091 policy-v1.json
( cd "$HARNESS" && python3 replay.py --target http://localhost:8091 --token $TOKEN \
    --cases ../corpus/battery.jsonl ../corpus/singles.jsonl ../corpus/sequences-short.jsonl \
            ../corpus/sequences-long.jsonl ../corpus/pending.jsonl --max-diffs 40 ) \
  | tee "$EVID/1_replay_v1.txt"

# --- 2. PROBE RUNNER: baseline vs v1
log "=== STEP 2: probe baseline (ronda_larga) vs v1 ==="
( cd "$HARNESS" && python3 probes.py --target http://localhost:8091 --token $TOKEN \
    --sondas-file "$SN" --baseline ) | tee "$EVID/2_probes_baseline.txt"
REF="$LAB/probes_baseline_localhost_8091.jsonl"

# --- 3. CHANGE EMULATORS
log "=== STEP 3: mutated emulators (:8092-8095) ==="
launch 8092 policy-a-transfer80k.json
launch 8093 policy-b-block5.json
launch 8094 policy-c-e506.json
launch 8095 policy-d-saldo1000.json

# --- 4. DETECTION: battery + probes vs each mutant
log "=== STEP 4: detection ==="
for pc in "8092:A" "8093:B" "8094:C" "8095:D"; do
  p=${pc%%:*}; m=${pc##*:}
  { echo "## mutant $m (:$p), battery of 27"
    cd "$HARNESS" && python3 monitor.py --once --target http://localhost:$p --token $TOKEN 2>&1 | grep -vE '^\[sondas\]'
    echo "## mutant $m (:$p), probes vs baseline v1"
    python3 probes.py --target http://localhost:$p --token $TOKEN --sondas-file "$SN" --ref "$REF" 2>&1 | grep -E 'target=|CAMBIARON|sin cambios'
  } | tee "$EVID/4_deteccion_$m.txt"
done

# --- 5. LOCALIZATION AND ADAPTATION (mutation A)
log "=== STEP 5: bisect limit A + adapt v2 ==="
( cd "$HARNESS" && python3 - <<'PY'
import httpx, time, json, sys, os
sys.path.insert(0, os.path.join('..','experiments')); import control
PORT=8092; c=httpx.Client(base_url=f'http://localhost:{PORT}',headers={'Authorization':'Bearer lab'},timeout=15)
n=[0]
def send(p): n[0]+=1; return c.post('/msg',json={'m':control.message(p)})
def reset(): n[0]+=1; c.post('/__reset')
def setup(): reset(); send('ALTA;L'); send('ALTA;M'); send('DEPOSITO;AC-0001;9999999999')
def over(A):
    setup(); r=send(f'TRANSFER;AC-0001;AC-0002;{A}')
    try: e=r.json().get('error')
    except Exception: e=None
    return e=='E506'
t0=time.time(); tr=[]
for A in [50000,100000,200000,500000,1000000]: tr.append((A,over(A)))
lo=max(A for A,o in tr if not o); hi=min(A for A,o in tr if o)
while hi-lo>1:
    mid=(lo+hi)//2
    if over(mid): hi=mid
    else: lo=mid
dt=time.time()-t0; c.close()
json.dump({'limit':lo,'requests':n[0],'seconds':round(dt,3)}, open('../lab/bisect_A.json','w'))
print(f'limite_exacto={lo} requests={n[0]} seg={dt:.3f}')
PY
) | tee "$EVID/5_bisect.txt"

LIMIT=$(python3 -c "import json;print(json.load(open('$LAB/bisect_A.json'))['limit'])")
python3 - "$ROOT" "$LAB" "$LIMIT" <<'PY'
import json, sys
root, lab, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
d=json.load(open(f'{root}/rules/policy-v1.json')); d['version']='v2-lab'
d['limits']['transferMax']['value']=limit
d['limits']['transferMax']['nota']=f'inclusive {limit}; lab bisection vs :8092'
json.dump(d, open(f'{lab}/policy-v2-lab.json','w'), ensure_ascii=False, indent=2)
print('policy-v2-lab.json limit=',limit)
PY
cp "$LAB/policy-v2-lab.json" "$POL/policy-v2-lab.json"
launch 8096 policy-v2-lab.json
log "re-recording corpus vs mutant 8092 -> lab/corpus-v2, replay vs v2 (8096)"
( cd "$HARNESS" && python3 record.py all --target http://localhost:8092 --token $TOKEN \
    --out-dir ../lab/corpus-v2 --budget 2000 >/dev/null 2>&1
  python3 replay.py --target http://localhost:8096 --token $TOKEN \
    --cases ../lab/corpus-v2/battery.jsonl ../lab/corpus-v2/singles.jsonl \
            ../lab/corpus-v2/sequences-short.jsonl ../lab/corpus-v2/sequences-long.jsonl \
            ../lab/corpus-v2/pending.jsonl --max-diffs 20 ) | tee "$EVID/5_replay_v2.txt"

# --- 6. FALSE ALARMS vs v1 (3 rounds)
log "=== STEP 6: false alarms (3 rounds vs v1) ==="
( cd "$HARNESS" && python3 monitor.py --rounds 3 --interval 0 --target http://localhost:8091 \
    --token $TOKEN 2>&1 | grep -vE '^\[sondas\]' | grep -iE 'ronda|battery|ALERTA|fin' ) \
  | tee "$EVID/6_falsas_alarmas.txt"

# --- 6b. Contract check + noise floor + latency + metrics (new features)
log "=== STEP 6b: contract + noise + latency + metrics vs v1 ==="
( cd "$HARNESS" && python3 monitor.py --once --long --noise 3 \
    --target http://localhost:8091 --token $TOKEN 2>&1 \
    | grep -iE 'contract|noise|latency|metrics|budget' ) \
  | tee "$EVID/6b_contrato_ruido.txt"

# --- 6c. INFRA vs service classification (local stub: 503 + X-Frontier-Infra +
#         "Application not found"); must classify INFRA, NOT alert, and retry.
log "=== STEP 6c: INFRA classification (local stub :8198) ==="
python3 - <<'PY' &
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _r(self):
        self.send_response(503); self.send_header("X-Frontier-Infra", "1"); self.end_headers()
        self.wfile.write(b'{"message":"Application not found"}')
    do_GET = do_POST = lambda s: s._r()
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", 8198), H).serve_forever()
PY
STUB=$!; sleep 1
( cd "$HARNESS" && python3 monitor.py --once --target http://localhost:8198 \
    --token $TOKEN --infra-wait 1 2>&1 | grep -iE 'contrato|battery|infra|latencia' ) \
  | tee "$EVID/6c_infra.txt"
kill "$STUB" 2>/dev/null

# --- 6d. PARTIAL re-recording (--from-diff): replaying the v1 corpus vs mutant A
#         produces diffs; record re-records only those cases and merges them.
log "=== STEP 6d: partial re-record --from-diff (mutant A :8092) ==="
( cd "$HARNESS" && python3 replay.py --target http://localhost:8092 --token $TOKEN \
    --cases ../corpus/battery.jsonl ../corpus/singles.jsonl --max-diffs 40 \
    > "$EVID/6d_diff.txt" 2>&1 || true
  python3 record.py --target http://localhost:8092 --token $TOKEN \
    --out-dir ../lab/corpus-v2 --from-diff "$EVID/6d_diff.txt" 2>&1 \
    | grep -iE 'from-diff|partial|merge|spent' ) | tee "$EVID/6d_regrabado_parcial.txt"
log "per-round metrics in harness/metrics.jsonl; daily budget of the original in harness/budget.json"

# --- 7. REPORT: pass/fail summary in lab/REPORTE_run.md
log "=== writing lab/REPORTE_run.md ==="
python3 - "$LAB" "$EVID" <<'PY'
import os, re, sys, json
lab, evid = sys.argv[1], sys.argv[2]
def grab(f):
    p=os.path.join(evid,f); return open(p).read() if os.path.exists(p) else ''
r1=grab('1_replay_v1.txt'); m=re.search(r'iguales=\d+ \(([\d.]+)%\).*casos 100%=(\d+)/(\d+)', r1, re.S)
pct=m.group(1) if m else '?'; cp=f'{m.group(2)}/{m.group(3)}' if m else '?'
bis=json.load(open(f'{lab}/bisect_A.json')) if os.path.exists(f'{lab}/bisect_A.json') else {}
r5=grab('5_replay_v2.txt'); m5=re.search(r'\(([\d.]+)%\).*casos 100%=(\d+)/(\d+)', r5, re.S)
pct5=m5.group(1) if m5 else '?'; cp5=f'{m5.group(2)}/{m5.group(3)}' if m5 else '?'
r6=grab('6_falsas_alarmas.txt'); alerts=r6.count('ALERTA')
det={}
for mx in 'ABCD':
    t=grab(f'4_deteccion_{mx}.txt')
    battery_ok = 'ALERTA DERIVA' in t
    mm=re.search(r'(\d+) sondas CAMBIARON', t)
    det[mx]=(battery_ok, mm.group(1) if mm else '0')
out=[]
out.append('# Lab report (generated by run_lab.sh)\n')
out.append(f'- Paso 1 replay v1: {pct}% pasos, casos 100% {cp}')
out.append(f'- Paso 2 baseline sondas: {"OK" if os.path.exists(f"{lab}/probes_baseline_localhost_8091.jsonl") else "FALTA"}')
for mx in 'ABCD':
    b,s=det[mx]; out.append(f'- Step 4 detection {mx}: battery {"DETECTS" if b else "no"}, changed probes {s}')
out.append(f'- Step 5 bisection of limit A: {bis.get("limit","?")} in {bis.get("requests","?")} requests ({bis.get("seconds","?")}s)')
out.append(f'- Paso 5 replay corpus re-grabado vs v2: {pct5}% pasos, casos 100% {cp5}')
out.append(f'- Step 6 false alarms (3 rounds v1): {alerts} alerts (expected 0)')
open(f'{lab}/REPORTE_run.md','w').write('\n'.join(out)+'\n')
print('\n'.join(out))
PY

log "DONE. Evidence in lab/evidence/, summary in lab/REPORTE_run.md"

#!/bin/zsh
# One-command release of a policy version with the replay gate built in.
#
#   bash control-plane/harness/release.sh v2 [corpus-dir]
#
# 1. Builds the jar and starts it locally with POLICY=<version>.
# 2. Replays the corpus recorded against that version of the original; any diff aborts the release.
# 3. Sets POLICY=<version> on the Railway service and deploys the current replica/ directory.
# 4. Waits for the deployment and replays the same corpus against the public entrypoint.
# Never run it while a hidden-set evaluation is in progress: the evaluator and this script share one ledger.
set -euo pipefail
VERSION="${1:?usage: release.sh <policy-version> [corpus-dir]}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CORPUS="${2:-$ROOT/control-plane/corpus}"
[ "$VERSION" = "v1" ] || [ -d "$2" ] 2>/dev/null || CORPUS="$ROOT/control-plane/corpus-$VERSION"
SERVICE="frontier-reto2-replica"
PUBLIC="https://frontier-reto2-replica-production.up.railway.app"
set -a; source "$ROOT/.env"; set +a
CASES=("$CORPUS"/battery.jsonl "$CORPUS"/singles.jsonl "$CORPUS"/sequences-short.jsonl "$CORPUS"/sequences-long.jsonl "$CORPUS"/pending.jsonl)

echo "== 1/4 build + local gate (policy $VERSION, corpus $CORPUS)"
(cd "$ROOT/replica" && gradle shadowJar -x test -q)
PORT=8099 TEAM_TOKEN="$TEAM_TOKEN" POLICY="$VERSION" java -jar "$ROOT/replica/build/libs/app.jar" > /tmp/release-local.out 2>&1 &
JAR_PID=$!
trap 'kill $JAR_PID 2>/dev/null || true' EXIT
sleep 4
python3 "$ROOT/control-plane/harness/replay.py" --target http://localhost:8099 --token "$TEAM_TOKEN" --cases "${CASES[@]}" --max-diffs 10 | tee /tmp/release-gate.out | grep -vE '^\[[0-9]{4}\]' | tail -8
grep -qE 'equal=[0-9]+ \(100\.00%\)' /tmp/release-gate.out || { echo "!! local gate FAILED: the replica with $VERSION does not reproduce $CORPUS; release aborted"; exit 1; }
kill $JAR_PID 2>/dev/null || true

echo "== 2/4 deploy (POLICY=$VERSION)"
(cd "$ROOT/replica" && railway variable set "POLICY=$VERSION" --service "$SERVICE" --skip-deploys)
# DEPLOY_DIR lets you deploy a prepared copy (for example with local-only patches) instead of replica/.
(cd "$ROOT/replica" && railway up --detach --service "$SERVICE" ${DEPLOY_DIR:+--path-as-root "$DEPLOY_DIR"})

echo "== 3/4 wait for the deployment"
for i in $(seq 1 60); do
  STATUS=$(cd "$ROOT/replica" && railway deployment list --service "$SERVICE" --json 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d[0]["status"] if d else "?")' 2>/dev/null || echo "?")
  echo "   $(date +%H:%M:%S) $STATUS"
  case "$STATUS" in SUCCESS) break;; FAILED|CRASHED) echo "!! deployment $STATUS"; exit 1;; esac
  sleep 10
done

echo "== 4/4 public verification"
python3 "$ROOT/control-plane/harness/replay.py" --target "$PUBLIC" --token "$TEAM_TOKEN" --cases "${CASES[@]}" --max-diffs 10 | grep -vE '^\[[0-9]{4}\]' | tail -8
echo "== release of $VERSION finished"

#!/bin/bash
# Dual-pool A/B: SAME model (Qwen3-8B) in two pools (A=2 pods, B=3 pods), three routing arms
# over one standup. Swaps ONLY the HTTPRoute per arm, runs the single-harness sweep, streams
# the full IPP log live, collects logs + slim extract. Install the matching IPP values BEFORE
# each arm (helm upgrade -f ...): weighted arms -> dual-pool-weighted-values.yaml (1 model,
# pass-through); smart arm -> dual-pool-smart-values.yaml (2 aliases + scorer).
#   NAMESPACE=<your-namespace> ab_pool_split_run.sh <arm> <route-mode> [profile=poisson_rps_pyramid.yaml]
#     route-mode: w5050 (A50/B50) | w4060 (A40/B60, pod-ratio optimal) | w6040 (A60/B40) | smart (IPP scorer)
set -u
ARM="${1:?arm}"; MODE="${2:?route-mode: w5050|smart|w6040}"; PROFILE="${3:-poisson_rps_pyramid.yaml}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
NS="${NAMESPACE:?set NAMESPACE to your namespace}"; DAP=access-to-harness-data-workload-pvc
MA=Qwen/Qwen3-8B-a; MB=Qwen/Qwen3-8B-b
D=ipp_benchmarking/example_outputs/ocp-qwen3-8b-dual-pool
DEST=$REPO/$D/$ARM
OCD=$DEST/oc-logs; STOP=/tmp/aps_${ARM}_stop
cd "$REPO"; source .venv/bin/activate 2>/dev/null; source .env 2>/dev/null
: "${LLMDBENCH_WAIT_TIMEOUT:=6000}"; export LLMDBENCH_WAIT_TIMEOUT
mkdir -p "$OCD"
rm -f "$STOP"; : > "$DEST/ipp-full-live.log"

idlabel() { local m="${1//\//-}"; m="${m//./-}"
  local h; h=$(printf '%s/%s' "$NS" "$m" | sha256sum | cut -c1-8)
  printf '%s-%s-%s' "${m:0:8}" "$h" "${m: -8}" | tr '[:upper:]' '[:lower:]'; }
LA=$(idlabel "$MA"); LB=$(idlabel "$MB")

# 0. Decode pools idle down to 0 replicas between sessions -> scale A=2 / B=3 and wait for
#    all 5 vLLM pods Ready (readiness probe = /health = serving) before driving load.
oc scale deploy "${LA}-decode" -n $NS --replicas=2 2>/dev/null
oc scale deploy "${LB}-decode" -n $NS --replicas=3 2>/dev/null
echo "waiting for 5 decode pods Ready..."
n=0
for _ in $(seq 1 72); do
  n=$(oc get pods -n $NS --no-headers 2>/dev/null | grep -E "$LA-decode|$LB-decode" | grep -c '2/2 *Running')
  [ "$n" -ge 5 ] && break
  sleep 5
done
echo "decode pods 2/2 Running: $n/5"
[ "$n" -ge 5 ] || { echo "ABORT: decode pools not ready"; exit 1; }

# 1. Swap routing for this arm: drop any of our routes, apply the mode's route(s).
oc delete httproute -n $NS qwen3-8b-a-route qwen3-8b-b-route qwen3-8b-dual-pool-split 2>/dev/null
case "$MODE" in
  w5050) ipp_benchmarking/tools/gen_weighted_route.sh "$NS" 1 1 | oc apply -f - ;;
  w4060) ipp_benchmarking/tools/gen_weighted_route.sh "$NS" 2 3 | oc apply -f - ;;  # A=40% B=60% (pod-ratio optimal)
  w6040) ipp_benchmarking/tools/gen_weighted_route.sh "$NS" 3 2 | oc apply -f - ;;  # A=60% B=40%
  smart) ipp_benchmarking/tools/gen_httproutes.sh    "$NS" "" "$MA" "$MB" | oc apply -f - ;;
  *) echo "bad route-mode $MODE"; exit 1 ;;
esac
sleep 3
# Lift the per-route 30s request timeout so nothing is shed (delta is latency, not 504s).
for r in $(oc get httproute -n $NS -o name | grep -E 'qwen3-8b-(a|b)-route|dual-pool-split'); do
  oc patch "$r" -n $NS --type=json -p='[{"op":"replace","path":"/spec/rules/0/timeouts/request","value":"1200s"}]' 2>/dev/null
done

POD=$(oc get pods -n $NS --no-headers | grep payload-processor | grep Running | awk '{print $1}' | head -1)
DA=$(oc get pods -n $NS --no-headers | grep "$LA" | grep decode | grep Running | awk '{print $1}' | head -1)
DB=$(oc get pods -n $NS --no-headers | grep "$LB" | grep decode | grep Running | awk '{print $1}' | head -1)
echo "ARM=$ARM MODE=$MODE IPP=$POD poolA($LA)=$DA poolB($LB)=$DB"
oc delete pod -n $NS $DAP --force --grace-period=0 2>/dev/null

# 2. Stream the full IPP log live (survives kubelet container-log rotation at v=4 under load).
( while [ ! -f "$STOP" ]; do oc logs -f -n $NS "$POD" --since=5s 2>/dev/null >> "$DEST/ipp-full-live.log"; done ) & N3=$!
echo "fulllog=$N3"

# 3. single-harness run (weighted or smart route splits the load across both pools)
nohup llmdbenchmark --spec cicd/ocp-qwen3-8b-dual-pool-run run -l inference-perf -w "$PROFILE" -p $NS > /tmp/aps_${ARM}_summ.log 2>&1 & SUMM=$!
echo "run=$SUMM"

# 4. wait for the run to finish
while ! grep -q "All pods completed successfully" /tmp/aps_${ARM}_summ.log 2>/dev/null; do
  kill -0 "$SUMM" 2>/dev/null || { echo "run exited"; break; }
  sleep 30
done
echo "WAITER done"; sleep 15
touch "$STOP"; sleep 25; kill $N3 2>/dev/null

# 5. Collect stage/summary/per-request from the WORKLOAD PVC. On OCP the harness writes
#    results to workload-pvc (seen as /requests in the data-access pod), NOT to the local
#    render workspace -- so discover the results dir via the DAP, not via find on $SWS.
SWS=$(grep -oE '/tmp/workspace_llmdbench_[^/]+/[[:alnum:]_.-]+-[0-9]{8}-[0-9]+-[0-9]+' /tmp/aps_${ARM}_summ.log | head -1)
# DAP is force-deleted at run start & llmdbench may not leave it -> recreate from the plan manifest.
if ! oc get pod -n $NS $DAP >/dev/null 2>&1; then
  APM=$(find "$SWS" -name 06_pod_access_to_harness_data.yaml 2>/dev/null | head -1)
  [ -n "$APM" ] && oc apply -n $NS -f "$APM" >/dev/null 2>&1
fi
oc wait --for=condition=Ready pod/$DAP -n $NS --timeout=90s >/dev/null 2>&1
# newest results dir on the PVC (named inference-perf-<epoch>-<rand>_1, NOT arad-<ts>)
EXP=$(oc exec -n $NS $DAP -- sh -c 'ls -1t /requests 2>/dev/null | head -1' 2>/dev/null | tr -d '\r')
echo "PVC results dir: ${EXP:-<none>}"
if [ -n "$EXP" ]; then
  oc exec -n $NS $DAP -- sh -c "cd /requests/$EXP && tar cf - stage_*_lifecycle_metrics.json summary_lifecycle_metrics.json 2>/dev/null" | tar xf - -C "$DEST" 2>/dev/null
  oc exec -n $NS $DAP -- sh -c "cat /requests/$EXP/stdout.log 2>/dev/null" > "$DEST/harness_stdout.log" 2>/dev/null
  # slim extract in-pod (per_request file can be >10GB); raw stays on the PVC (too big to keep local)
  oc cp ipp_benchmarking/tools/extract_per_request_slim.py $NS/$DAP:/tmp/extract.py >/dev/null 2>&1
  oc exec -n $NS $DAP -- python3 /tmp/extract.py "/requests/$EXP/per_request_lifecycle_metrics.json" /tmp/slim.json 2>&1 | tail -1
  oc cp $NS/$DAP:/tmp/slim.json "$DEST/per_request_slim.json" >/dev/null 2>&1
  echo "raw per_request left on workload-pvc at /requests/$EXP/ (recover via the access pod)" > "$DEST/per_request_RAW_location.txt"
else
  echo "WARN: no results dir on workload-pvc -- collection incomplete"
fi

# 6. collect logs (routing breakdown on-demand: analyze_routing.py on ipp-full-live.log)
oc logs -n $NS "$POD" --tail=-1 > "$OCD/ipp-tail.log" 2>/dev/null
oc logs -n $NS "$DA" -c vllm --tail=-1 > "$OCD/decode-poolA-vllm.log" 2>/dev/null
oc logs -n $NS "$DB" -c vllm --tail=-1 > "$OCD/decode-poolB-vllm.log" 2>/dev/null
oc delete pods -n $NS -l app=llmdbench-harness-launcher --force --grace-period=0 2>/dev/null
NAMESPACE=$NS bash ipp_benchmarking/collect_logs.sh > /tmp/aps_${ARM}_collect.log 2>&1
BUN=$(grep -oE 'collected-logs-[0-9]+' /tmp/aps_${ARM}_collect.log | head -1)
[ -n "$BUN" ] && [ -d "$REPO/$BUN" ] && mv "$REPO/$BUN" "$REPO/collected-logs-dual-pool-$ARM" 2>/dev/null

echo "APS_DONE_$ARM"

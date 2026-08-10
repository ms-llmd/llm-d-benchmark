#!/bin/bash
# Routing A/B: median-TTFT scorer (smart) vs single-model runs. One arm/invocation.
# Configure the IPP for the arm per the README (helm upgrade -f <values>) first.
#   ab_routing_run.sh <arm> <ipp_values_file> [profile=half_8b_poisson.yaml]
set -u
ARM="${1:?arm}"; CFG="${2:?ipp values file}"; PROFILE="${3:-half_8b_poisson.yaml}"; SPEC="${4:-cicd/ocp-qwen3-8b-32b-summarizer}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
NS="${NAMESPACE:?set NAMESPACE to your namespace}"; DAP=access-to-harness-data-workload-pvc
D=ipp_benchmarking/example_outputs/ocp-research-agent-routing
DEST=$REPO/$D/$ARM
OCD=$DEST/oc-logs; STOP=/tmp/abr_${ARM}_stop
cd "$REPO"; source .venv/bin/activate 2>/dev/null; source .env 2>/dev/null
: "${LLMDBENCH_WAIT_TIMEOUT:=6000}"; export LLMDBENCH_WAIT_TIMEOUT   # poisson runs are long; else per-request data is lost
mkdir -p "$OCD"
rm -f "$STOP"; : > "$DEST/ipp-full-live.log"

# 1. IPP must already be configured for this arm (helm upgrade -f $CFG), per the README.
# No timeouts: lift the per-route 30s request timeout (the sole load-shedder) so requests
# complete instead of recording 504s. Survives across runs; re-applied per arm to be safe.
for r in $(oc get httproute -n $NS -o name | grep -E 'qwen3-(8b|32b)'); do
  oc patch "$r" -n $NS --type=json -p='[{"op":"replace","path":"/spec/rules/0/timeouts/request","value":"1200s"}]' 2>/dev/null
done
POD=$(oc get pods -n $NS --no-headers | grep payload-processor | grep Running | awk '{print $1}' | head -1)
D8=$(oc get pods -n $NS --no-headers | grep 'qwen3-8b-decode' | grep Running | awk '{print $1}' | head -1)
D32=$(oc get pods -n $NS --no-headers | grep 'wen3-32b-decode' | grep Running | awk '{print $1}' | head -1)
echo "ARM=$ARM CFG=$CFG IPP=$POD"
oc delete pod -n $NS $DAP --force --grace-period=0 2>/dev/null   # let llmdbenchmark own the data-access pod

# 2. capture the full IPP log (ALL lines incl. ttft-observation / queue-ttft score) streamed live so
# it survives container-log rotation (kubelet caps ~250Mi; at v=4 under load it rotates in minutes,
# so --tail at run end loses early stages). The only complete per-request source; its "Model selected"
# lines drive the routing analysis/plots (analyze_routing.py).
( while [ ! -f "$STOP" ]; do oc logs -f -n $NS "$POD" --since=5s 2>/dev/null >> "$DEST/ipp-full-live.log"; done ) & N3=$!
echo "fulllog=$N3"

# 3. summarizer harness (no planner)
nohup llmdbenchmark --spec "$SPEC" run -l inference-perf -w "$PROFILE" -p $NS > /tmp/abr_${ARM}_summ.log 2>&1 & SUMM=$!
echo "summarizer=$SUMM"

# 4. wait for summarizer to finish
while ! grep -q "All pods completed successfully" /tmp/abr_${ARM}_summ.log 2>/dev/null; do
  kill -0 "$SUMM" 2>/dev/null || { echo "summarizer exited"; break; }
  sleep 30
done
echo "WAITER done"; sleep 15
touch "$STOP"; sleep 25; kill $N3 2>/dev/null

# 5. stage files + slim
SWS=$(grep -oE '/tmp/workspace_llmdbench_[^/]+/[[:alnum:]_.-]+-[0-9]{8}-[0-9]+-[0-9]+' /tmp/abr_${ARM}_summ.log | head -1)
SDIR=$(find "$SWS" -name stage_0_lifecycle_metrics.json 2>/dev/null | head -1 | xargs dirname 2>/dev/null)
cp "$SDIR"/stage_*_lifecycle_metrics.json "$SDIR"/summary_lifecycle_metrics.json "$DEST/" 2>/dev/null
# inference-perf load-gen stdout (the "Stage N - run started" lines) -> stage bands for the TTFT plotter
find "$SWS" -path '*/logs/inference-perf-*.log' 2>/dev/null | head -1 | xargs -r cp -t "$DEST" 2>/dev/null && \
  mv "$DEST"/inference-perf-*.log "$DEST/harness_stdout.log" 2>/dev/null
SEXP=$(basename "$SDIR" 2>/dev/null | sed 's/_1$//')
oc cp ipp_benchmarking/tools/extract_per_request_slim.py $NS/$DAP:/tmp/extract.py >/dev/null 2>&1
oc exec -n $NS $DAP -- python3 /tmp/extract.py "/requests/${SEXP}_1/per_request_lifecycle_metrics.json" /tmp/slim.json 2>&1
oc cp $NS/$DAP:/tmp/slim.json "$DEST/per_request_slim.json" >/dev/null 2>&1
# full raw per-request file too (100MB-1GB, usually truncated at the tail -> parse tolerantly:
# zcat file.gz | extract_per_request_slim.py - out.json). gzip out of the pod; stays local, don't commit.
oc exec -n $NS $DAP -- gzip -c "/requests/${SEXP}_1/per_request_lifecycle_metrics.json" \
  > "$DEST/per_request_lifecycle_metrics.json.gz" 2>/dev/null

# 6. collect logs (routing breakdown is on-demand: analyze_routing.py on ipp-full-live.log)
oc logs -n $NS "$POD" --tail=-1 > "$OCD/ipp-tail.log" 2>/dev/null
oc logs -n $NS "$D8" -c vllm --tail=-1 > "$OCD/decode-8b-vllm.log" 2>/dev/null
oc logs -n $NS "$D32" -c vllm --tail=-1 > "$OCD/decode-32b-vllm.log" 2>/dev/null
oc delete pods -n $NS -l app=llmdbench-harness-launcher --force --grace-period=0 2>/dev/null
NAMESPACE=$NS bash ipp_benchmarking/collect_logs.sh > /tmp/abr_${ARM}_collect.log 2>&1
BUN=$(grep -oE 'collected-logs-[0-9]+' /tmp/abr_${ARM}_collect.log | head -1)
[ -n "$BUN" ] && [ -d "$REPO/$BUN" ] && mv "$REPO/$BUN" "$REPO/collected-logs-routing-$ARM" 2>/dev/null

echo "ABR_DONE_$ARM"

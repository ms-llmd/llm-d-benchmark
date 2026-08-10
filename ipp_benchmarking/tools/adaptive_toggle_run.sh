#!/bin/bash
# Gemma/Qwen ADAPTIVE-ROUTING experiment driver. Runs the 7-stage toggle timeline as DISCRETE,
# NON-OVERLAPPING stages: each stage launches exactly its own inference-perf run(s) at its start,
# waits for ALL of them to finish, then the next stage begins. Nothing crosses a stage boundary.
#
#   Stage        Shared  Gemma-only  Qwen-only   runs launched this stage
#   -1 baseline1 off     10          10          gemma + qwen
#    0 baseline2 10      off         off         shared
#    1 pin Gemma 10      10          off         shared + gemma
#    2 release   10      off         off         shared
#    3 pin Qwen  10      off         10          shared + qwen
#    4 both      10      10          10          shared + gemma + qwen
#    5 release   10      off         off         shared
#
# All three streams: 10 rps, 300s, IDENTICAL summarization shape (~2048 in / ~256 out) and the same
# tokenizer, so a shared request and a pinned request are the same unit of work and per-pool
# latencies are directly comparable. Rates/durations live in each profile's `stages:` block.
# NOTE stage 4 is deliberately oversubscribed: 30 rps offered against ~25 rps of combined capacity
# (Gemma ~11.1 measured, Qwen ~14 predicted at this shape), so expect queue growth there.
# Uses the SINGLE-STACK run spec (ocp-gemma-qwen-adaptive-run) so each llmdbenchmark run executes
# the profile ONCE (the 2-stack standup scenario would double-run it).
#
# Each concurrent run gets its OWN harness namespace (-p <model_ns>,<harness_ns>): llmdbenchmark
# deletes pods by the shared label app=llmdbench-harness-launcher and rewrites the
# inference-perf-profiles ConfigMap in its harness namespace, so two runs sharing one namespace
# truncate each other. Model/endpoint discovery still targets $NS.
#
# PREREQS (see README.md section 3): standup (2-stack scenario) + IPP (-expl values) +
# base-model ConfigMaps + gen_httproutes.sh. Then: adaptive_toggle_run.sh [tag]
set -u
TAG="${1:-adaptive}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
NS="${NAMESPACE:?set NAMESPACE to your namespace}"; DAP=access-to-harness-data-workload-pvc
SPEC=cicd/ocp-gemma-qwen-adaptive-run     # single stack -> one execution per run
ROOT="$REPO/ipp_benchmarking/example_outputs/gemma-qwen-adaptive/$TAG"
# Pinned profiles depend on how the IPP build pins. model-group-name-filter builds take an exact
# model name (the defaults); auto-group-model-name-filter builds take "auto/<group>" and reject
# exact names with "Filter eliminated all models" -> every pinned request fails. For those:
#   GEMMA=adaptive_gemma_autogroup_summarization.yaml QWEN=adaptive_qwen_autogroup_summarization.yaml
SHARED=${SHARED:-adaptive_shared_summarization.yaml}
GEMMA=${GEMMA:-adaptive_gemma_summarization.yaml}
QWEN=${QWEN:-adaptive_qwen_summarization.yaml}
declare -A HNS=([$SHARED]=$NS [$GEMMA]=$NS-2 [$QWEN]=$NS-3)   # one harness namespace per profile
STAGGER=5                        # s between two same-stage launches (just API-call spacing now)

cd "$REPO"; source .venv/bin/activate 2>/dev/null; source .env 2>/dev/null
: "${LLMDBENCH_WAIT_TIMEOUT:=6000}"; export LLMDBENCH_WAIT_TIMEOUT
STOP=/tmp/adapt_${TAG}_stop; rm -f "$STOP"; mkdir -p "$ROOT"; : > "$ROOT/stage_marks.log"

# Seed each extra harness namespace. `run` cannot do this itself: the SA+RBAC is a standup-only
# step, and its own HarnessNamespaceStep (which would make the PVC) is a non-per-stack step that
# the executor defers until AFTER the per-stack DeployHarnessStep -- so the harness pod would hang
# Pending on a missing workload-pvc. Everything here is idempotent.
for hns in $(printf '%s\n' "${HNS[@]}" | sort -u); do
  [ "$hns" = "$NS" ] && continue
  oc create ns "$hns" --dry-run=client -o yaml | oc apply -f - >/dev/null
  oc apply -n "$hns" -f - >/dev/null <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata: {name: workload-pvc}
spec:
  accessModes: [ReadWriteMany]
  resources: {requests: {storage: 96Gi}}
---
apiVersion: v1
kind: Pod
metadata:
  name: access-to-harness-data-workload-pvc
  labels: {app: llm-d-benchmark-harness, role: llm-d-benchmark-data-access}
spec:
  containers:
  - name: rsync
    image: ghcr.io/llm-d/llm-d-benchmark:v0.7.0
    command: ["rsync","--daemon","--no-detach","--port=20873","--log-file=/dev/stdout"]
    volumeMounts: [{name: workload, mountPath: /requests}]
  volumes: [{name: workload, persistentVolumeClaim: {claimName: workload-pvc}}]
---
apiVersion: v1
kind: ServiceAccount
metadata: {name: inference-perf-runner}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata: {name: inference-perf-job-creator}
rules:
- {apiGroups: ["batch"], resources: ["jobs"], verbs: ["create","get","list","watch","delete","patch","update"]}
- {apiGroups: [""], resources: ["serviceaccounts"], verbs: ["get"]}
- {apiGroups: [""], resources: ["pods"], verbs: ["get","list","watch"]}
- {apiGroups: [""], resources: ["pods/log"], verbs: ["get"]}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: inference-perf-job-creator-binding}
subjects: [{kind: ServiceAccount, name: inference-perf-runner, namespace: $hns}]
roleRef: {kind: Role, name: inference-perf-job-creator, apiGroup: rbac.authorization.k8s.io}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: inference-perf-restricted-scc}
subjects: [{kind: ServiceAccount, name: inference-perf-runner, namespace: $hns}]
roleRef: {kind: ClusterRole, name: system:openshift:scc:restricted, apiGroup: rbac.authorization.k8s.io}
EOF
  oc wait --for=jsonpath='{.status.phase}'=Bound pvc/workload-pvc -n "$hns" --timeout=300s >/dev/null
  oc wait --for=condition=Ready pod -l role=llm-d-benchmark-data-access -n "$hns" --timeout=300s >/dev/null
done

# lift the per-route 30s request timeout so long requests complete instead of recording 504s
for r in $(oc get httproute -n $NS -o name 2>/dev/null | grep -iE 'gemma|qwen'); do
  oc patch "$r" -n $NS --type=json -p='[{"op":"replace","path":"/spec/rules/0/timeouts/request","value":"1200s"}]' 2>/dev/null
done

# IPP capture comes from the IN-CLUSTER ipp-logtail pod (tails payload-processor BY LABEL into
# /requests/smart-logs/ipp-full-live.log on the PVC), not from a laptop-side `oc logs -f`: measured
# 100% of routed requests versus 24.9%. The bottleneck was the laptop's link, not the kubelet.
# Windows are sliced in-pod after the timeline (see ipp_extract_stage.sh).
oc get pod ipp-logtail -n $NS --no-headers 2>/dev/null | grep -q Running \
  || echo "*** WARN: ipp-logtail is not Running -- this run will have NO IPP record ***"

# Per-stage Gemma/Qwen split comes from the vLLM completion counters, not the IPP log: the log
# stream is lossy (`oc logs -f` drops ~90% of lines at 30+ rps). Fails loudly -- a silent 0 from a
# broken exec is indistinguishable from a real zero and would corrupt the delta.
ctr() {  # ctr <decode-deploy-prefix>
  local pod v
  pod=$(oc get pod -n $NS --no-headers | grep "^${1}-" | awk '$2~/^[1-9]/{print $1;exit}')
  [ -z "$pod" ] && { echo "CTR_ERR no ready pod for $1" >&2; return 1; }
  v=$(oc exec -n $NS "$pod" -c vllm -- curl -s localhost:8000/metrics 2>/dev/null \
    | awk '/^vllm:request_success_total.*engine="0".*finished_reason="length"/{s+=$2; n++} END{if(n)print s; else print ""}')
  [ -z "$v" ] && { echo "CTR_ERR no metric from $pod" >&2; return 1; }
  echo "$v"
}
# Deploy names are {first8}-{sha256(ns/model)[:8]}-{last8}-decode, so they change with the
# namespace -- derive them (same idlabel as gen_httproutes.sh) instead of pinning a hash.
idlabel() { local m="${1//\//-}"; m="${m//./-}"
  local h; h=$(printf '%s/%s' "$NS" "$m" | sha256sum | cut -c1-8)
  printf '%s-%s-%s' "${m:0:8}" "$h" "${m: -8}" | tr '[:upper:]' '[:lower:]'; }
G_DEPLOY="${G_DEPLOY:-$(idlabel RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic)-decode}"
Q_DEPLOY="${Q_DEPLOY:-$(idlabel Qwen/Qwen3.6-35B-A3B-FP8)-decode}"
echo "stage,gemma,qwen,total,gemma_pct" > "$ROOT/splits.csv"

# Qwen3.6 (hybrid GDN+attention) can wedge SILENTLY under load: engine stops stepping, 100% GPU,
# zero tokens, requests pegged at max_num_seqs, pod still Ready and error-free. Every later stage
# would then record garbage. Only token progress reveals it, so gate each stage on it and abort.
# See example_outputs/gemma-qwen-adaptive/qwen-engine-wedge/.
ready_pod() { oc get pod -n $NS --no-headers 2>/dev/null | grep "^${1}-" | awk '$2~/^[1-9]/{print $1;exit}'; }
# echoes "<generation_tokens_total> <num_requests_running>" for a pod, empty if unreachable
pool_state() {
  [ -z "$1" ] && return
  oc exec -n $NS "$1" -c vllm -- curl -s localhost:8000/metrics 2>/dev/null \
    | awk '/^vllm:generation_tokens_total/{g=$2} /^vllm:num_requests_running/{r=int($2)} END{if(g!="")print g, r+0}'
}
assert_progress() {  # assert_progress <label> -- both pools must still be able to generate
  local lbl="$1" d pod s0 s1 g0 g1 n
  for d in "$G_DEPLOY" "$Q_DEPLOY"; do
    pod=$(ready_pod "$d"); [ -z "$pod" ] && continue
    s0=$(pool_state "$pod"); [ -z "$s0" ] && continue
    g0=${s0% *}; n=${s0#* }
    [ "${n:-0}" -eq 0 ] && continue          # idle pool cannot be wedged
    sleep 20
    s1=$(pool_state "$pod"); [ -z "$s1" ] && continue
    g1=${s1% *}
    if [ "$g0" = "$g1" ]; then
      echo "*** ABORT during $lbl: $d WEDGED (running=$n, generation_tokens_total frozen at $g0) ***"
      echo "$lbl WEDGED deploy=$d gen_tokens=$g0 running=$n" >> "$ROOT/stage_marks.log"
      touch "$STOP"; return 1
    fi
  done
  return 0
}

LAST_PID=0
launch() {  # launch <profile> <label>
  local prof="$1" label="$2"
  nohup llmdbenchmark --spec "$SPEC" run -l inference-perf -w "$prof" -p "$NS,${HNS[$prof]}" \
    > "/tmp/adapt_${TAG}_${label}.log" 2>&1 & LAST_PID=$!
  echo "    +$((SECONDS-T0))s launched $label ($prof) pid=$LAST_PID"
}

run_stage() {  # run_stage <label> <profile...>  -- launch all, wait for ALL, then return
  local label="$1"; shift
  echo "===== STAGE $label START +$((SECONDS-T0))s -- runs: $* ====="
  # split declaration from assignment, else `local` masks ctr's exit status and a broken
  # exec is recorded as a real zero -- exactly the silent corruption ctr() promises to avoid
  local g0 q0; g0=$(ctr "$G_DEPLOY") || return 1; q0=$(ctr "$Q_DEPLOY") || return 1
  echo "$label start_ts=$(date +%s) offset=$((SECONDS-T0)) g0=$g0 q0=$q0" >> "$ROOT/stage_marks.log"
  local pids=() first=1
  for prof in "$@"; do
    [ $first -eq 1 ] || sleep $STAGGER; first=0
    launch "$prof" "${label}_${prof%%.*}"; pids+=($LAST_PID)
  done
  # Poll for completion, and every ~60s check the pools are still generating. The check must run
  # DURING load: once a stage ends the pools are idle and a wedge is indistinguishable from quiet.
  local tick=0
  while :; do
    local live=0; for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && live=$((live+1)); done
    [ "$live" -eq 0 ] && break
    tick=$((tick+1))
    if [ $((tick % 4)) -eq 0 ] && ! assert_progress "$label"; then
      for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
      return 1
    fi
    sleep 15
  done
  local g1 q1; g1=$(ctr "$G_DEPLOY") || return 1; q1=$(ctr "$Q_DEPLOY") || return 1
  echo "$label end_ts=$(date +%s) offset=$((SECONDS-T0)) g1=$g1 q1=$q1" >> "$ROOT/stage_marks.log"
  awk -v l="$label" -v g=$((${g1%.*}-${g0%.*})) -v q=$((${q1%.*}-${q0%.*})) \
    'BEGIN{t=g+q; printf "%s,%d,%d,%d,%s\n", l, g, q, t, (t?sprintf("%.1f",100*g/t):"NA")}' \
    | tee -a "$ROOT/splits.csv"
  # A stage that ran load but completed nothing means every request failed (commonly a pinned
  # profile whose model name the filter rejects). The harness still reports "Run complete", so
  # without this the whole timeline runs to the end and only the flat splits.csv reveals it.
  if [ $(( ${g1%.*} - ${g0%.*} + ${q1%.*} - ${q0%.*} )) -eq 0 ]; then
    echo "*** $label completed ZERO requests -- check the profiles' model names against the"
    echo "*** IPP filter ('Filter eliminated all models' in the IPP log) before rerunning."
    return 1
  fi
  echo "===== STAGE $label DONE +$((SECONDS-T0))s ====="
}

T0=$SECONDS
# Stop the whole timeline on a wedge -- every later stage would otherwise record garbage against a
# dead engine, which is exactly how two earlier saturation ramps were silently invalidated.
die() { echo "TIMELINE ABORTED at $1 -- see $ROOT/stage_marks.log"; touch "$STOP"; exit 1; }
run_stage "s-1_baseline1" "$GEMMA" "$QWEN"        || die s-1_baseline1
run_stage "s0_baseline2"  "$SHARED"               || die s0_baseline2
run_stage "s1_pinGemma"   "$SHARED" "$GEMMA"      || die s1_pinGemma
run_stage "s2_release"    "$SHARED"               || die s2_release
run_stage "s3_pinQwen"    "$SHARED" "$QWEN"       || die s3_pinQwen
run_stage "s4_both"       "$SHARED" "$GEMMA" "$QWEN" || die s4_both
run_stage "s5_release"    "$SHARED"               || die s5_release

echo "all stages done in $((SECONDS-T0))s"; touch "$STOP"

# Slice every stage window out of the in-cluster capture: per-stage routing log + per-request e2e
# csv. +60s past each end_ts so requests still in flight at the boundary are not dropped.
WINDOWS=$(awk '/start_ts=/{split($2,a,"=");s[$1]=a[2]} /end_ts=/{split($2,a,"=");if($1 in s)printf "%s:%s:%s ",$1,s[$1],a[2]+60}' "$ROOT/stage_marks.log")
if [ -n "$WINDOWS" ]; then
  oc exec -i -n $NS "$DAP" -- sh -c 'cat > /requests/ipp_extract_stage.sh' < "$REPO/ipp_benchmarking/tools/ipp_extract_stage.sh"
  oc exec -n $NS "$DAP" -- sh /requests/ipp_extract_stage.sh $WINDOWS
  for lbl in $(echo "$WINDOWS" | tr ' ' '\n' | cut -d: -f1); do
    mkdir -p "$ROOT/$lbl"
    oc exec -n $NS "$DAP" -- cat "/requests/ipp-stage-slices/$lbl.ipp.log" > "$ROOT/$lbl/ipp-decisions.log" 2>/dev/null
    oc exec -n $NS "$DAP" -- cat "/requests/ipp-stage-slices/$lbl.e2e.csv"  > "$ROOT/$lbl/e2e.csv" 2>/dev/null
  done
fi

cp /tmp/adapt_${TAG}_*.log "$ROOT/" 2>/dev/null
NAMESPACE=$NS bash ipp_benchmarking/collect_logs.sh > "/tmp/adapt_${TAG}_collect.log" 2>&1
BUN=$(grep -oE 'collected-logs-[0-9]+' "/tmp/adapt_${TAG}_collect.log" | head -1)
[ -n "$BUN" ] && [ -d "$REPO/$BUN" ] && mv "$REPO/$BUN" "$REPO/collected-logs-adaptive-$TAG" 2>/dev/null
echo "ADAPTIVE_DONE_$TAG total=$((SECONDS-T0))s out=$ROOT"

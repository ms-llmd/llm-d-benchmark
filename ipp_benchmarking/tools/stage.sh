#!/bin/bash
# stage.sh <label> <profile>...   -- run ONE stage of the adaptive experiment end to end.
#
# Runs the stage, then archives EVERYTHING for it in one go: per-stream harness results, the IPP
# routing slice, and the per-request e2e csv. Previously the archiving was a separate script that
# was easy to forget.
#
# Each profile becomes its own llmdbenchmark run in its OWN harness namespace (two runs sharing a
# namespace delete each other's harness pod). The Gemma/Qwen split comes from the vLLM completion
# counters, which are authoritative; the IPP log is the per-request record.
#
# RESILIENT TO THE gpu-reaper: a cluster CronJob (*/5 min, 90-min idle threshold, DCGM util < 5%)
# scales our idle GPU deploys to 0 and has fired MID-STAGE. We cannot exclude our namespace, so:
#   - pre-flight refuses to start unless both pools serve HTTP 200
#   - the wait loop checks pod identity every ~30 s and aborts within a minute of a swap
#   - the whole attempt is retried automatically (ATTEMPTS) after the pool comes back (~10 min)
set -u
LABEL="$1"; shift
PROFILES=("$@")
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
cd "$REPO" || exit 1
source .venv/bin/activate 2>/dev/null
source .env 2>/dev/null
export LLMDBENCH_WAIT_TIMEOUT=6000
NS=${NS:?set NS to your namespace}
SPEC=${SPEC:-cicd/ocp-gemma-qwen-adaptive-run}
OUT="$REPO/ipp_benchmarking/example_outputs/gemma-qwen-adaptive/${TAG:-stage-300s-summarization}"
mkdir -p "$OUT"
# deploy names are {first8}-{sha256(ns/model)[:8]}-{last8}-decode, so they change with the
# namespace -- derive them (same idlabel as gen_httproutes.sh) instead of pinning a hash
idlabel() { local m="${1//\//-}"; m="${m//./-}"
  local h; h=$(printf '%s/%s' "$NS" "$m" | sha256sum | cut -c1-8)
  printf '%s-%s-%s' "${m:0:8}" "$h" "${m: -8}" | tr '[:upper:]' '[:lower:]'; }
G_DEPLOY=${G_DEPLOY:-$(idlabel RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic)-decode}
Q_DEPLOY=${Q_DEPLOY:-$(idlabel Qwen/Qwen3.6-35B-A3B-FP8)-decode}
ATTEMPTS=${ATTEMPTS:-3}
DAP_POD=access-to-harness-data-workload-pvc   # data-access pod that mounts the PVC with the logtail capture
EXTRACTOR=$REPO/ipp_benchmarking/tools/ipp_extract_stage.sh
TAIL_PAD=60                                   # s of window past stage end: in-flight requests still finishing
START_TS=0

hns() { case "$1" in
  adaptive_shared_summarization.yaml) echo "$NS" ;;
  adaptive_shared_summ20.yaml)        echo "$NS" ;;
  adaptive_gemma_summarization.yaml)  echo "$NS-2" ;;
  adaptive_gemma_autogroup_summarization.yaml) echo "$NS-2" ;;
  adaptive_qwen_summarization.yaml)   echo "$NS-3" ;;
  adaptive_qwen_autogroup_summarization.yaml) echo "$NS-3" ;;
esac; }
pod_of()  { oc get pod -n $NS --no-headers 2>/dev/null | grep "^${1}-" | awk '$2~/^[1-9]/{print $1;exit}'; }
serving() { oc exec -n $NS "$1" -c vllm -- curl -s -o /dev/null -w '%{http_code}' localhost:8000/v1/models 2>/dev/null; }

ctr() {  # completed-request counter; dies rather than returning a silent 0
  local pod v
  pod=$(pod_of "$1"); [ -z "$pod" ] && { echo "CTR_ERR no ready pod for $1" >&2; return 1; }
  v=$(oc exec -n $NS "$pod" -c vllm -- curl -s localhost:8000/metrics 2>/dev/null \
    | awk '/^vllm:request_success_total.*engine="0".*finished_reason="length"/{s+=$2;n++} END{if(n)print s; else print ""}')
  [ -z "$v" ] && { echo "CTR_ERR no metric from $pod" >&2; return 1; }
  echo "${v%.*}"
}
gen() {  # "<generation_tokens_total> <num_requests_running>"
  local pod; pod=$(pod_of "$1"); [ -z "$pod" ] && return
  oc exec -n $NS "$pod" -c vllm -- curl -s localhost:8000/metrics 2>/dev/null \
    | awk '/^vllm:generation_tokens_total/{g=$2} /^vllm:num_requests_running/{r=int($2)} END{if(g!="")print g, r+0}'
}

wait_pools() {  # block until both pools serve, so a retry starts from a healthy cluster
  local i p c
  for i in $(seq 1 60); do
    local ok=1
    for d in $G_DEPLOY $Q_DEPLOY; do
      p=$(pod_of "$d"); [ -z "$p" ] && { ok=0; break; }
      c=$(serving "$p"); [ "$c" != "200" ] && { ok=0; break; }
    done
    [ "$ok" = "1" ] && { echo "  pools healthy $(date +%T)"; return 0; }
    echo "  waiting for pools to serve... $(date +%T)"
    sleep 30
  done
  return 1
}

archive() {  # archive <start_ts> <end_ts> <total_routed> -- results + IPP slice + per-request e2e
  local lo="$1" hi="$2" tot="$3" d ws t prof sub ipp_n

  # harness result dirs whose workspace was created inside this stage's window
  for d in /tmp/workspace_llmdbench_*/*/results/*/; do
    # workspace dirs are <user>-YYYYMMDD-HHMMSS-<ms>; key off the timestamp, not the username
    ws=$(echo "$d" | grep -oE '[0-9]{8}-[0-9]{6}' | head -1); [ -z "$ws" ] && continue
    t=$(date -d "$(echo "$ws" | sed -E 's/([0-9]{4})([0-9]{2})([0-9]{2})-([0-9]{2})([0-9]{2})([0-9]{2})/\1-\2-\3 \4:\5:\6/')" +%s 2>/dev/null) || continue
    [ "$t" -lt "$((lo-180))" ] && continue
    [ "$t" -gt "$hi" ] && continue
    prof=$(ls "$d" | grep '^adaptive.*yaml$' | head -1); [ -z "$prof" ] && continue
    case "$prof" in *shared*) sub=shared;; *gemma*) sub=gemma;; *qwen*) sub=qwen;; *) sub=other;; esac
    mkdir -p "$OUT/$LABEL/$sub"
    # epp/igw/modelserving_pods.log are 35-53 MB each and contain ZERO routing lines
    rsync -a --exclude 'epp_pods.log' --exclude 'igw_pods.log' --exclude 'modelserving_pods.log' \
      "$d" "$OUT/$LABEL/$sub/" 2>/dev/null
    echo "  archived $sub <- $d"
  done
  cp /tmp/stage_${LABEL}_*.log "$OUT/$LABEL/" 2>/dev/null

  # IPP slice + per-request e2e, both from the in-cluster logtail capture. Measured 100% coverage
  # versus 24.9% for a laptop-side `oc logs -f`. Push the extractor each time: the pod's /tmp does
  # not survive a restart.
  oc exec -i -n $NS "$DAP_POD" -- sh -c 'cat > /requests/ipp_extract_stage.sh' < "$EXTRACTOR" || return 0
  oc exec -n $NS "$DAP_POD" -- sh /requests/ipp_extract_stage.sh "$LABEL:$lo:$((hi+TAIL_PAD))"
  oc exec -n $NS "$DAP_POD" -- cat "/requests/ipp-stage-slices/$LABEL.ipp.log" \
    > "$OUT/$LABEL/ipp-decisions.log" 2>/dev/null
  oc exec -n $NS "$DAP_POD" -- cat "/requests/ipp-stage-slices/$LABEL.e2e.csv" \
    > "$OUT/$LABEL/e2e.csv" 2>/dev/null
  ipp_n=$(grep -c '"msg":"Model selected"' "$OUT/$LABEL/ipp-decisions.log" 2>/dev/null || echo 0)
  echo "  ipp capture: $ipp_n of $tot routed = $(awk -v a="$ipp_n" -v b="$tot" 'BEGIN{if(b)printf "%.1f", 100*a/b; else print "NA"}')%"
  echo "  e2e rows:    $(( $(wc -l < "$OUT/$LABEL/e2e.csv" 2>/dev/null || echo 1) - 1 ))"

  verify "$ipp_n" "$tot"
}

# Every stage must end with, per run: the PER-REQUEST lifecycle metrics, the harness stdout, and
# the stage's IPP slice. A missing one is only discoverable now -- the data is gone once the next
# stage overwrites the workspace -- so say so loudly rather than finding out at analysis time.
verify() {
  local ipp_n="$1" tot="$2" sub d fat n miss=0
  for sub in $(ls "$OUT/$LABEL" 2>/dev/null | grep -E '^(shared|gemma|qwen)$'); do
    d="$OUT/$LABEL/$sub"
    fat=$(ls "$d"/*per_request*lifecycle*metrics*.json 2>/dev/null | head -1)
    if [ -n "$fat" ]; then
      # the harness truncates this file mid-write; the slimmer recovers every complete record
      python3 "$REPO/ipp_benchmarking/tools/extract_per_request_slim.py" "$fat" "$d/per_request_slim.json" >/dev/null 2>&1
      n=$(python3 -c "import json;print(len(json.load(open('$d/per_request_slim.json'))))" 2>/dev/null || echo 0)
      echo "  [$sub] per-request: $n records ($(du -h "$fat" | cut -f1) raw -> per_request_slim.json)"
      [ "${n:-0}" -lt 100 ] && { echo "  [$sub] *** per-request record count looks wrong ***"; miss=1; }
    else
      echo "  [$sub] *** MISSING per-request lifecycle metrics ***"; miss=1
    fi
    [ -s "$d/stdout.log" ] && echo "  [$sub] harness stdout: $(wc -l < "$d/stdout.log") lines" \
                           || { echo "  [$sub] *** MISSING harness stdout ***"; miss=1; }
    [ -s "$d/stage_0_lifecycle_metrics.json" ] || { echo "  [$sub] *** MISSING stage lifecycle metrics ***"; miss=1; }
  done
  awk -v a="$ipp_n" -v b="$tot" 'BEGIN{if(b && 100*a/b < 95) exit 1}' \
    || { echo "  *** IPP capture below 95% -- check ipp-logtail ***"; miss=1; }
  [ "$miss" = "1" ] && echo "===== $LABEL CAPTURE INCOMPLETE -- rerun the stage before moving on ====="
  return 0
}

attempt() {
  local G_POD0 Q_POD0 G0 Q0 pids=() first=1 tick=0 live p prof c
  for d in $G_DEPLOY $Q_DEPLOY; do
    p=$(pod_of "$d")
    [ -z "$p" ] && { echo "PREFLIGHT FAIL: no ready pod for $d (reaped?)"; return 1; }
    c=$(serving "$p")
    [ "$c" != "200" ] && { echo "PREFLIGHT FAIL: $d pod $p not serving (HTTP ${c:-none})"; return 1; }
    echo "  preflight ok: $d -> $p"
  done
  G_POD0=$(pod_of $G_DEPLOY); Q_POD0=$(pod_of $Q_DEPLOY)
  G0=$(ctr $G_DEPLOY) || return 1
  Q0=$(ctr $Q_DEPLOY) || return 1
  echo "start counters: gemma=$G0 qwen=$Q0"
  START_TS=$(date +%s)
  echo "$LABEL start_ts=$START_TS g0=$G0 q0=$Q0" >> "$OUT/stage_marks.log"

  # the in-cluster logtail must actually be capturing, or the stage has no IPP record
  if ! oc get pod ipp-logtail -n $NS --no-headers 2>/dev/null | grep -q Running; then
    echo "  WARN: ipp-logtail pod is not Running -- no IPP capture for this stage"
  fi

  for prof in "${PROFILES[@]}"; do
    [ $first -eq 1 ] || sleep 5; first=0
    nohup llmdbenchmark --spec "$SPEC" run -l inference-perf -w "$prof" -p "$NS,$(hns "$prof")" \
      > "/tmp/stage_${LABEL}_${prof%%.*}.log" 2>&1 &
    pids+=($!)
    echo "  launched $prof (ns=$(hns "$prof")) pid=${pids[-1]}"
  done

  while :; do
    live=0; for p in "${pids[@]}"; do kill -0 "$p" 2>/dev/null && live=$((live+1)); done
    [ "$live" -eq 0 ] && break
    tick=$((tick+1))
    # abort FAST on a reap rather than at the end of the stage
    if [ $((tick % 2)) -eq 0 ]; then
      if [ "$(pod_of $G_DEPLOY)" != "$G_POD0" ] || [ "$(pod_of $Q_DEPLOY)" != "$Q_POD0" ]; then
        echo "  !! pod swapped mid-stage (reaper?) -- aborting attempt early"
        for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
        sleep 5; return 2
      fi
    fi
    if [ $((tick % 4)) -eq 0 ]; then   # silent-wedge guard (Qwen hybrid engine)
      for d in $G_DEPLOY $Q_DEPLOY; do
        read -r a n <<<"$(gen $d)"; [ "${n:-0}" -eq 0 ] && continue
        sleep 20; read -r b _ <<<"$(gen $d)"
        [ "$a" = "$b" ] && { echo "  !! $d WEDGED (gen_tokens frozen at $a)"; \
          for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done; return 3; }
      done
    fi
    sleep 15
  done

  if [ "$(pod_of $G_DEPLOY)" != "$G_POD0" ] || [ "$(pod_of $Q_DEPLOY)" != "$Q_POD0" ]; then
    echo "  !! pod swapped during stage -- counters reset, attempt invalid"; return 2
  fi
  local G1 Q1 dg dq tot pct END_TS
  G1=$(ctr $G_DEPLOY) || return 1
  Q1=$(ctr $Q_DEPLOY) || return 1
  dg=$((G1-G0)); dq=$((Q1-Q0)); tot=$((dg+dq))
  END_TS=$(date +%s)
  echo "$LABEL end_ts=$END_TS g1=$G1 q1=$Q1" >> "$OUT/stage_marks.log"

  archive "$START_TS" "$END_TS" "$tot"

  [ -f "$OUT/splits.csv" ] || echo "stage,gemma,qwen,total,gemma_pct" > "$OUT/splits.csv"
  pct=$([ $tot -gt 0 ] && awk -v g=$dg -v t=$tot 'BEGIN{printf "%.1f", 100*g/t}' || echo NA)
  echo "$LABEL,$dg,$dq,$tot,$pct" >> "$OUT/splits.csv"
  echo "===== STAGE $LABEL DONE $(date +%T)  gemma=$dg qwen=$dq total=$tot gemma_pct=$pct ====="
  return 0
}

for try in $(seq 1 $ATTEMPTS); do
  echo "===== STAGE $LABEL attempt $try/$ATTEMPTS  profiles: ${PROFILES[*]}  $(date +%T) ====="
  attempt && exit 0
  rc=$?
  echo "  attempt $try failed (rc=$rc)"
  [ "$try" -eq "$ATTEMPTS" ] && { echo "===== STAGE $LABEL GAVE UP after $ATTEMPTS attempts ====="; exit $rc; }
  echo "  cleaning up harness pods and waiting for pools before retry..."
  for n in $NS $NS-2 $NS-3; do oc delete pod -n $n -l app=llmdbench-harness-launcher --ignore-not-found >/dev/null 2>&1; done
  wait_pools || { echo "===== STAGE $LABEL GAVE UP: pools never came back ====="; exit 4; }
done

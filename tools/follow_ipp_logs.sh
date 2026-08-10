#!/usr/bin/env bash
#
# Continuously stream the IPP (payload-processor) container logs to a local file
# so nothing is lost to kubelet container-log rotation during a benchmark run.
#
# The kubelet keeps only ~50 MiB of stdout per container on disk; at V(3) the IPP
# emits ~23 lines/request, so under load `kubectl logs` (post-run) only returns the
# most recent slice. A live `oc logs -f` follower receives every line as it is
# emitted, before rotation discards it.
#
# Start this BEFORE launching the run; stop it after (kill the PID, or Ctrl-C).
#
# Usage:
#   NAMESPACE=llm-d-arad ./tools/follow_ipp_logs.sh [output_file]
#
# Env:
#   NAMESPACE      target namespace            (default: llm-d-arad)
#   IPP_SELECTOR   pod label selector for IPP  (default: app=payload-processor)
set -uo pipefail

NS="${NAMESPACE:-llm-d-arad}"
SEL="${IPP_SELECTOR:-app=payload-processor}"
OUT="${1:-/tmp/ipp-run-logs/ipp-full-$(date +%Y%m%d-%H%M%S).log}"
mkdir -p "$(dirname "$OUT")"

echo "[follower] streaming '$SEL' in ns '$NS' -> $OUT" | tee -a "$OUT.err"

first=1
while true; do
  if [ "$first" -eq 1 ]; then
    # First attach: grab everything currently buffered (--tail=-1), then follow.
    oc logs -f -l "$SEL" -n "$NS" --timestamps --tail=-1 --max-log-requests=5 \
      >> "$OUT" 2>>"$OUT.err"
    first=0
  else
    # Reconnect after a stream drop / pod restart: only the last few seconds,
    # to minimise duplicate lines (timestamps allow dedup if needed).
    oc logs -f -l "$SEL" -n "$NS" --timestamps --since=5s --max-log-requests=5 \
      >> "$OUT" 2>>"$OUT.err"
  fi
  echo "[follower] stream ended; reconnecting at $(date -u +%FT%TZ)" >> "$OUT.err"
  sleep 1
done

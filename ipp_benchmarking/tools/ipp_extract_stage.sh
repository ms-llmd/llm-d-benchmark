#!/bin/sh
# Runs INSIDE the data-access pod, over the in-cluster ipp-logtail capture. One pass per invocation,
# two outputs per stage window:
#
#   $DEST/<label>.ipp.log   routing decisions + scorer reasoning + faults
#   $DEST/<label>.e2e.csv   rid,model,t0,t_end,e2e,shared -- PER-REQUEST e2e latency, attributed
#                           to the pool that served it. Validated against the harness lifecycle
#                           metrics to within 8 ms at p50.
#
# Handles BOTH IPP builds:
#   t_end   old: "Incoming response body chunk" EoS:true | new: last "ttft-observation" (emitted at
#           end of stream on the streaming path; 2 ms before the client sees the last byte)
#   shared  old: 2 "ttft-aware score" lines (both pools scored) vs 1 for pinned
#           new: "all candidates kept" (auto) vs "model-group filter applied explicit match" (pinned)
# The ttft-aware-p25-expl rebuild drops the per-chunk flood AND the "ttft-aware score" line, so
# predicted effectiveTTFT is not available on it at any --v.
#
# Windows are passed as: <label>:<start_ts>:<end_ts> ...
# Widen the end by ~60s past the last routing line or requests still in flight are lost.
LOG=${LOG:-/requests/smart-logs/ipp-full-live.log}
DEST=${DEST:-/requests/ipp-stage-slices}
mkdir -p "$DEST"
echo "windows: $*"

tail -c "${BYTES:-40000000000}" "$LOG" \
| grep -E -e '"msg":"Model selected"' -e 'ttft-aware score' -e 'ttft-observation' \
          -e 'candidateModels' -e '"level":"(error|warn|dpanic|panic|fatal)"' \
          -e 'no models available' -e 'captured request headers' -e '"EoS":true' \
          -e 'all candidates kept' -e 'model-group filter applied explicit match' \
| awk -v windows="$*" -v dest="$DEST" '
BEGIN {
  nw = split(windows, w, " ")
  for (i = 1; i <= nw; i++) {
    split(w[i], p, ":"); lbl[i] = p[1]; lo[i] = p[2] + 0; hi[i] = p[3] + 0
    ipp[i] = dest "/" p[1] ".ipp.log"; printf "" > ipp[i]
  }
}
{
  if (!match($0, /"ts":[0-9.]+/)) next
  t = substr($0, RSTART + 5, RLENGTH - 5) + 0
  k = 0
  for (i = 1; i <= nw; i++) if (t >= lo[i] && t <= hi[i]) { k = i; break }
  if (!k) next

  # --- routing / scorer record ---
  if (index($0, "\"msg\":\"Model selected\"") || index($0, "ttft-aware score") ||
      index($0, "ttft-observation") || index($0, "candidateModels") ||
      index($0, "no models available") || $0 ~ /"level":"(error|warn|dpanic|panic|fatal)"/) {
    print >> ipp[k]; cnt[k]++
  }

  # --- per-request lifecycle for the e2e csv ---
  if (!match($0, /"x-request-id":"[^"]+"/)) next
  key = k SUBSEP substr($0, RSTART + 16, RLENGTH - 17)
  if (index($0, "captured request headers"))                        { t0[key] = t }
  else if (index($0, "all candidates kept"))                        { sh[key] = 1 }
  else if (index($0, "applied explicit match"))                     { sh[key] = 0 }
  else if (index($0, "\"msg\":\"ttft-aware score\""))               { ns[key]++ }
  # last line of either kind wins: both are emitted at end of stream
  else if (index($0, "\"msg\":\"Incoming response body chunk\"") ||
           index($0, "\"msg\":\"ttft-observation\""))               { te[key] = t }
  else if (index($0, "\"msg\":\"Model selected\"")) {
    match($0, /"model":"[^"]+"/); m = substr($0, RSTART + 9, RLENGTH - 10)
    mdl[key] = (index(m, "gemma") ? "gemma" : (index(m, "Qwen") ? "qwen" : m))
  }
}
END {
  for (i = 1; i <= nw; i++) {
    f = dest "/" lbl[i] ".e2e.csv"
    print "rid,model,t0,t_end,e2e,shared" > f
    n = 0
    for (key in t0) {
      split(key, a, SUBSEP)
      if (a[1] + 0 != i || !(key in te) || !(key in mdl)) continue
      s = (key in sh) ? sh[key] : (ns[key] >= 2 ? 1 : 0)   # new build first, old build fallback
      printf "%s,%s,%.4f,%.4f,%.4f,%d\n", a[2], mdl[key], t0[key], te[key], te[key] - t0[key], s >> f
      n++
    }
    printf "%s ipp_lines=%d e2e_rows=%d\n", lbl[i], cnt[i], n
  }
}
'

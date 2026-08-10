#!/bin/bash
# oc's data path truncates large transfers at random byte counts on this cluster; the only reliable
# pull is to retry until gzip -t passes.  retry_gz.sh <ns> <remote-file> <local.gz> [tries]
set -u
NS=$1 SRC=$2 DST=$3 TRIES=${4:-8} DAP=access-to-harness-data-workload-pvc
want=$(oc exec -n "$NS" "$DAP" -- stat -c%s "$SRC" 2>/dev/null)
for i in $(seq "$TRIES"); do
  oc exec -n "$NS" "$DAP" -- gzip -c "$SRC" > "$DST" 2>/dev/null
  if gzip -t "$DST" 2>/dev/null; then
    got=$(gzip -dc "$DST" | wc -c)
    [ "$got" = "$want" ] && { echo "OK try=$i gz=$(stat -c%s "$DST") raw=$got"; exit 0; }
    echo "try=$i gzip-ok but size $got != $want"
  else
    echo "try=$i truncated at $(stat -c%s "$DST")"
  fi
done
echo "FAILED after $TRIES tries"; exit 1

#!/bin/sh
# slice_raw_window.sh <label>:<lo_ts>:<hi_ts> ...  -- run INSIDE the data-access pod.
# Cuts the live IPP capture down to each stage's window and gzips it, so only the stage's
# own lines get transferred off the PVC (the capture holds every arm ever run).
SRC=/requests/smart-logs/ipp-full-live.log
DEST=/requests/smart-logs/windows   # the only dir the logtail pod's UID can create in
mkdir -p "$DEST"
for w in "$@"; do
  lbl=$(echo "$w" | cut -d: -f1); lo=$(echo "$w" | cut -d: -f2); hi=$(echo "$w" | cut -d: -f3)
  awk -v lo="$lo" -v hi="$hi" 'match($0,/"ts":[0-9]+/){
      t=substr($0,RSTART+5,RLENGTH-5)+0; if (t>=lo && t<=hi) print }' "$SRC" | gzip > "$DEST/$lbl.raw.log.gz"
  echo "$lbl: $(gzip -dc "$DEST/$lbl.raw.log.gz" | wc -l) lines, $(gzip -dc "$DEST/$lbl.raw.log.gz" | grep -c '"msg":"Model selected"') Model selected"
done

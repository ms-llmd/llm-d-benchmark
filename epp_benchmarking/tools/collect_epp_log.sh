#!/usr/bin/env bash
# Drain the epp-logtail buffer into a local file and reset it, so each arm gets
# its own log. See router/logtail.yaml.
#
#   collect_epp_log.sh <router-ns> <out.log>
#
# The tail re-reads the current container log whenever kubelet rotation ends its
# follow, so exact duplicates are expected; they are dropped keeping the first
# occurrence, which preserves chronological order that a sort on these JSON
# lines would not (they start with "level", not "ts").
set -euo pipefail
ns=${1:?usage: collect_epp_log.sh <router-ns> <out.log>}
out=${2:?usage: collect_epp_log.sh <router-ns> <out.log>}

kubectl -n "$ns" exec epp-logtail -c tail -- cat /logs/epp.log \
  | awk '!seen[$0]++' > "$out"
kubectl -n "$ns" exec epp-logtail -c tail -- sh -c ': > /logs/epp.log'

# Distinct request IDs is the arm-agnostic completeness check: it should match
# the request count the harness reports for the shared stream, whichever
# scorers were configured. A shortfall means the capture lost lines.
printf '%s: %s lines, %s requests routed\n' \
  "$out" "$(wc -l < "$out")" \
  "$(grep -o '"x-request-id":"[^"]*"' "$out" | sort -u | wc -l)"

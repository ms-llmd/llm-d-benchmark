#!/bin/bash
# Routing smoke test for the dual-pool scenario: apply a weighted HTTPRoute, fire N requests
# at the gateway, and report how many each pool actually served (delta of vLLM
# request_success_total across each pool's decode pods). Verifies the weight split works
# BEFORE committing to a full run. Assumes standup + IPP (dual-pool-weighted-values.yaml)
# are already up and both pools' decode pods are Running.
#   NAMESPACE=<your-namespace> smoke_route_split.sh [N=20] [weightA=1] [weightB=1]
set -u
N="${1:-20}"; WA="${2:-1}"; WB="${3:-1}"
NS="${NAMESPACE:?set NAMESPACE to your namespace}"
REPO=$(cd "$(dirname "$0")/../.." && pwd)
MA=Qwen/Qwen3-8B-a; MB=Qwen/Qwen3-8B-b
GW="${GATEWAY:-infra-llmdbench-inference-gateway}-istio"   # Istio creates the Service with a -istio suffix
cd "$REPO"

idlabel() { local m="${1//\//-}"; m="${m//./-}"
  local h; h=$(printf '%s/%s' "$NS" "$m" | sha256sum | cut -c1-8)
  printf '%s-%s-%s' "${m:0:8}" "$h" "${m: -8}" | tr '[:upper:]' '[:lower:]'; }
LA=$(idlabel "$MA"); LB=$(idlabel "$MB")

pods_of() { oc get pods -n "$NS" --no-headers 2>/dev/null | grep "$1" | grep decode | grep Running | awk '{print $1}'; }
PA=$(pods_of "$LA"); PB=$(pods_of "$LB")
[ -z "$PA" ] && { echo "no Running pool-A ($LA) decode pods in $NS"; exit 1; }
[ -z "$PB" ] && { echo "no Running pool-B ($LB) decode pods in $NS"; exit 1; }
echo "poolA($LA) pods:"; echo "$PA" | sed 's/^/  /'
echo "poolB($LB) pods:"; echo "$PB" | sed 's/^/  /'

# sum vLLM request_success_total (all finished_reason series) on one pod's /metrics (port 8200)
pod_metric() {
  oc exec -n "$NS" "$1" -c vllm -- python3 -c \
"import urllib.request as u
d=u.urlopen('http://localhost:8200/metrics',timeout=5).read().decode()
print(int(sum(float(l.split()[-1]) for l in d.splitlines() if l.startswith('vllm:request_success_total') and not l.startswith('#'))))" 2>/dev/null || echo 0
}
pool_sum() { local t=0 p; for p in $1; do t=$((t + $(pod_metric "$p"))); done; echo "$t"; }

# apply the weighted route for this test
ipp_benchmarking/tools/gen_weighted_route.sh "$NS" "$WA" "$WB" | oc apply -f -
sleep 5

A0=$(pool_sum "$PA"); B0=$(pool_sum "$PB")
echo "before: poolA=$A0 poolB=$B0  -- sending $N requests (weights $WA/$WB) ..."

oc run smoke-curl-$$ --rm -i --restart=Never -n "$NS" --image=quay.io/fedora/fedora --labels=llm-d-benchmark/ephemeral=true -- \
  bash -c "for i in \$(seq $N); do curl -s -o /dev/null -w '%{http_code} ' -X POST http://$GW.$NS.svc:80/v1/completions -H 'Content-Type: application/json' -d '{\"model\":\"Qwen/Qwen3-8B\",\"prompt\":\"The capital of France is\",\"max_tokens\":5,\"temperature\":0}'; done; echo" 2>/dev/null
sleep 3

A1=$(pool_sum "$PA"); B1=$(pool_sum "$PB")
DA=$((A1 - A0)); DB=$((B1 - B0)); TOT=$((DA + DB))
echo
echo "==== ROUTING SPLIT (weights $WA/$WB, $N requests) ===="
echo "poolA served: $DA"
echo "poolB served: $DB"
echo "total served: $TOT  (expected ~$N)"
[ "$TOT" -gt 0 ] && printf "ratio A:B = %.0f%% : %.0f%%\n" "$(echo "100*$DA/$TOT" | bc -l)" "$(echo "100*$DB/$TOT" | bc -l)"

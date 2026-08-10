#!/usr/bin/env bash
# Emit ONE catch-all HTTPRoute that weight-splits traffic across the two same-model
# InferencePools (dual-pool scenario). No header match -> every request hits this rule
# and Envoy splits by weight; each pool's EPP then load-balances across its own pods.
# Pool router names are the deterministic {model_id_label}-router (same hash as
# render_plans.py / gen_httproutes.sh), so pass the namespace and it regenerates.
#   gen_weighted_route.sh <namespace> <weightA> <weightB> [gateway] [modelA] [modelB] | kubectl apply -f -
set -euo pipefail
ns="${1:?usage: gen_weighted_route.sh <namespace> <weightA> <weightB> [gateway] [modelA] [modelB]}"
wa="${2:?weightA}"; wb="${3:?weightB}"
gw="${4:-infra-llmdbench-inference-gateway}"
ma="${5:-Qwen/Qwen3-8B-a}"; mb="${6:-Qwen/Qwen3-8B-b}"

idlabel() { local m="${1//\//-}"; m="${m//./-}"
  local h; h=$(printf '%s/%s' "$ns" "$m" | sha256sum | cut -c1-8)
  printf '%s-%s-%s' "${m:0:8}" "$h" "${m: -8}" | tr '[:upper:]' '[:lower:]'; }

cat <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: qwen3-8b-dual-pool-split
  namespace: ${ns}
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: ${gw}
  rules:
    - backendRefs:
        - group: inference.networking.k8s.io
          kind: InferencePool
          name: $(idlabel "$ma")-router
          port: 8000
          weight: ${wa}
        - group: inference.networking.k8s.io
          kind: InferencePool
          name: $(idlabel "$mb")-router
          port: 8000
          weight: ${wb}
      timeouts:
        backendRequest: 0s
        request: 1200s
EOF

#!/usr/bin/env bash
# Emit header-match HTTPRoutes (X-Gateway-Base-Model-Name -> InferencePool) for
# the manual IPP data path (scenario sets httpRoute.enabled: false). The pool
# name is the deterministic model id label {first8}-{sha256(ns/model)[:8]}-{last8}
# -router (matches render_plans.py _model_id_label_filter / bash model_attribute),
# so no hashes need hand-editing -- pass the namespace and it regenerates.
#   gen_httproutes.sh <namespace> [gateway] [model ...] | kubectl apply -f -
set -euo pipefail
ns="${1:?usage: gen_httproutes.sh <namespace> [gateway] [model ...]}"
gw="${2:-infra-llmdbench-inference-gateway}"
shift; (($#)) && shift || true
models=("$@"); ((${#models[@]})) || models=(Qwen/Qwen3-8B Qwen/Qwen3-32B)

idlabel() { local m="${1//\//-}"; m="${m//./-}"
  local h; h=$(printf '%s/%s' "$ns" "$m" | sha256sum | cut -c1-8)
  printf '%s-%s-%s' "${m:0:8}" "$h" "${m: -8}" | tr '[:upper:]' '[:lower:]'; }

for model in "${models[@]}"; do
  route=$(printf '%s' "${model##*/}" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/-*$//')
  cat <<EOF
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: ${route}-route
  namespace: ${ns}
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: ${gw}
  rules:
    - matches:
        - headers:
            - type: Exact
              name: X-Gateway-Base-Model-Name
              value: ${model}
      backendRefs:
        - group: inference.networking.k8s.io
          kind: InferencePool
          name: $(idlabel "$model")-router
          port: 8000
          weight: 1
      timeouts:
        backendRequest: 0s
        request: 0s
---
EOF
done

#!/opt/homebrew/bin/bash
#
# ipp_deploy.sh -- build a local IPP image, load it into the kind cluster,
# render a scorer + model-cost-extractor values file, and helm-install the
# payload-processor on top of an already-stood-up ipp_benchmarking cluster.
#
# Follows the phase/color/sleep conventions of mac_colima_bootstrap.sh so a
# human can follow progress in the terminal.
#
# Scorer selection (SCORER env, default `costguard`):
#   * `costguard` -- CostGuard scorer with epoch-based cost centroids.
#                    Values file: ipp_configs/kind-costguard/costguard-kind-values.yaml
#   * `costaware` -- cost-scorer plugin (stateless input-token pricing).
#                    Values file: ipp_configs/kind-costaware/costaware-kind-values.yaml
# The `model-cost-extractor` extractor is present under BOTH scorers -- costguard
# consumes its per-request events for epoch bookkeeping; cost-scorer reads the
# same TokenPricesAttributeKey the extractor populates.
#
# Preconditions:
#   * mac_colima_bootstrap.sh has run successfully (kind cluster + standup +
#     Gateway exist in namespace $NAMESPACE).
#   * IPP_PATH points at an llm-d-inference-payload-processor checkout with
#     a working `make image-kind` target.
#
# Usage:
#   IPP_PATH=/path/to/llm-d-inference-payload-processor \
#     ./ipp_benchmarking/tools/ipp_deploy.sh
#
# Environment overrides (all optional):
#   SCORER              costguard | costaware, default: costguard
#   KIND_CLUSTER_NAME   kind cluster to target, default: ipp-e2e (must match
#                       the bootstrap's KIND_CLUSTER_NAME)
#   NAMESPACE           k8s namespace with the standup, default: llmdbench
#   IPP_IMAGE_REPO      image repo the chart references,
#                       default: ghcr.io/llm-d/llm-d-inference-payload-processor
#   IPP_IMAGE_TAG       image tag the chart references, default: e2e (matches
#                       IPP Makefile's E2E_IMAGE ?= $(IMAGE):e2e)
#   IPP_VALUES          Helm values file, default: scorer-specific path (see
#                       above). Auto-generated when missing; user overrides
#                       are respected. The file carries both the scorer and
#                       the model-cost-extractor extractor -- no separate
#                       values file is needed.
#   RELEASE             Helm release name, default: payload-processor
#
# All generated artifacts live under ipp_benchmarking/ipp_configs/kind-<scorer>/
# so nothing is written into the IPP repo checkout at $IPP_PATH -- $IPP_PATH is
# used ONLY for reads (make image-kind, helm chart path).
#
# Idempotent -- re-runs upgrade in place.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCORER="${SCORER:-costguard}"
case "$SCORER" in
  costguard|costaware) ;;
  *)
    echo "❌ SCORER must be 'costguard' or 'costaware', got: '$SCORER'" >&2
    exit 2
    ;;
esac

KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-ipp-e2e}"
NAMESPACE="${NAMESPACE:-llmdbench}"
IPP_IMAGE_REPO="${IPP_IMAGE_REPO:-ghcr.io/llm-d/llm-d-inference-payload-processor}"

# Split fully-qualified image into registry + name for the IPP chart's
# `payloadProcessor.image.{registry,repository,tag}` schema. The chart template
# composes `{{ .registry }}/{{ .repository }}:{{ .tag }}` -- if we passed the
# full path as `.repository` the result would be doubled (e.g.
# `ghcr.io/llm-d/ghcr.io/llm-d/llm-d-inference-payload-processor:e2e`) and
# combined with pullPolicy=Never that would `ErrImageNeverPull` immediately.
# Everything up to the last `/` is the registry; the tail is the repository.
IPP_IMAGE_REGISTRY="${IPP_IMAGE_REPO%/*}"
IPP_IMAGE_NAME="${IPP_IMAGE_REPO##*/}"
IPP_IMAGE_TAG="${IPP_IMAGE_TAG:-e2e}"
RELEASE="${RELEASE:-payload-processor}"

# The inference Gateway RESOURCE name comes from the scenario's Helm release
# (infra-<release>-inference-gateway) and is what the IPP chart's
# `inferenceGateway.name` value expects (the EnvoyFilter targetRef binds to
# kind: Gateway with this name).
#
# Istio's controller creates a companion SERVICE with the "-istio" suffix
# (infra-llmdbench-inference-gateway-istio) that we use for preflight
# reachability checks and the LB IP lookup. These are TWO different names
# for the same logical Gateway -- do not conflate them.
GATEWAY_NAME="infra-${NAMESPACE}-inference-gateway"
GATEWAY_SVC="${GATEWAY_NAME}-istio"

# Kind's control-plane container name is derived from the cluster name.
KIND_CONTROL_PLANE="${KIND_CLUSTER_NAME}-control-plane"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

show_help() {
  cat <<EOF
Usage: IPP_PATH=/path/to/llm-d-inference-payload-processor $0 [-h|--help]

Builds and deploys the payload-processor Helm chart with the selected
scorer (SCORER=costguard|costaware) plus the model-cost-extractor
extractor into the kind cluster stood up by mac_colima_bootstrap.sh.

Required environment:
  IPP_PATH             path to the llm-d-inference-payload-processor checkout

Optional environment (defaults in parentheses):
  SCORER               scorer to deploy               (costguard)
                       Accepted: costguard | costaware
  KIND_CLUSTER_NAME    kind cluster name              (ipp-e2e)
  NAMESPACE            k8s namespace                  (llmdbench)
  IPP_IMAGE_REPO       image repo the chart uses      (ghcr.io/llm-d/llm-d-inference-payload-processor)
  IPP_IMAGE_TAG        image tag the chart uses       (e2e)
  IPP_VALUES           Helm values file               (ipp_configs/kind-\$SCORER/\$SCORER-kind-values.yaml)
  RELEASE              Helm release name              (payload-processor)

Flags:
  -h, --help           Show this help.
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) show_help; exit 0 ;;
    *) echo -e "${RED}▶ unknown argument: $arg${NC}"; show_help; exit 2 ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
echo -e "${GREEN}▶ Preflight...${NC}"

# Resolve BUNDLE_DIR = the ipp_benchmarking/ directory (parent of tools/).
# All generated artifacts live under $BUNDLE_DIR so nothing is written into
# $IPP_PATH -- IPP checkout stays a pure read-only source for us.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$BUNDLE_DIR/.." && pwd)"

if [[ -z "${IPP_PATH:-}" ]]; then
  echo -e "${RED}▶ FAIL: IPP_PATH is unset.${NC}"
  echo -e "${RED}▶ export IPP_PATH=/path/to/llm-d-inference-payload-processor${NC}"
  exit 1
fi
if [[ ! -d "$IPP_PATH" ]]; then
  echo -e "${RED}▶ FAIL: IPP_PATH does not exist or is not a directory: $IPP_PATH${NC}"
  exit 1
fi
if [[ ! -f "$IPP_PATH/Makefile" ]]; then
  echo -e "${RED}▶ FAIL: no Makefile at $IPP_PATH/Makefile -- is this the IPP repo?${NC}"
  exit 1
fi
if [[ ! -d "$IPP_PATH/config/charts/payload-processor" ]]; then
  echo -e "${RED}▶ FAIL: chart not found at $IPP_PATH/config/charts/payload-processor${NC}"
  exit 1
fi

# Scorer-specific default. User-set IPP_VALUES wins (existing precedence).
IPP_VALUES="${IPP_VALUES:-$BUNDLE_DIR/ipp_configs/kind-${SCORER}/${SCORER}-kind-values.yaml}"

# Verify the kind cluster this script targets actually exists.
if ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
  echo -e "${RED}▶ FAIL: kind cluster '${KIND_CLUSTER_NAME}' does not exist.${NC}"
  echo -e "${RED}▶ Run ipp_benchmarking/tools/mac_colima_bootstrap.sh first${NC}"
  echo -e "${RED}▶ (or export KIND_CLUSTER_NAME=<your-cluster> and re-try).${NC}"
  exit 1
fi

# Verify kubectl context points at that cluster.
current_ctx=$(kubectl config current-context 2>/dev/null || true)
if [[ "$current_ctx" != "kind-${KIND_CLUSTER_NAME}" ]]; then
  echo -e "${YELLOW}▶ kubectl context is '${current_ctx}', switching to 'kind-${KIND_CLUSTER_NAME}'.${NC}"
  kubectl config use-context "kind-${KIND_CLUSTER_NAME}" >/dev/null
fi

# Verify the Gateway svc exists (proves the standup ran).
if ! kubectl get svc "$GATEWAY_SVC" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo -e "${RED}▶ FAIL: Gateway svc '${GATEWAY_SVC}' not found in ns/${NAMESPACE}.${NC}"
  echo -e "${RED}▶ The ipp_benchmarking standup has not run yet.${NC}"
  echo -e "${RED}▶ Run ipp_benchmarking/tools/mac_colima_bootstrap.sh first.${NC}"
  exit 1
fi

echo -e "${GREEN}▶   SCORER:             $SCORER${NC}"
echo -e "${GREEN}▶   IPP_PATH:           $IPP_PATH${NC}"
echo -e "${GREEN}▶   KIND_CLUSTER_NAME:  $KIND_CLUSTER_NAME${NC}"
echo -e "${GREEN}▶   NAMESPACE:          $NAMESPACE${NC}"
echo -e "${GREEN}▶   IPP_IMAGE_REPO:     $IPP_IMAGE_REPO${NC}"
echo -e "${GREEN}▶   IPP_IMAGE_TAG:      $IPP_IMAGE_TAG${NC}"
echo -e "${GREEN}▶   IPP_VALUES:         $IPP_VALUES${NC}"
echo -e "${GREEN}▶   Gateway svc:        $GATEWAY_SVC (present)${NC}"
sleep 2

# ---------------------------------------------------------------------------
# Phase A -- clean stale IPP images from the kind node registry
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase A -- clean stale IPP images from ${KIND_CONTROL_PLANE}...${NC}"
sleep 2

# `crictl images` output columns: REPOSITORY TAG IMAGE_ID SIZE
# Match on repository substring so any prior local tag (e2e, latest, dev...) is caught.
stale_refs=$(docker exec "$KIND_CONTROL_PLANE" crictl images 2>/dev/null \
  | awk 'NR>1 && $1 ~ /llm-d-inference-payload-processor/ {print $1":"$2}' \
  | sort -u || true)

if [[ -z "$stale_refs" ]]; then
  echo -e "${GREEN}▶   no stale IPP images on the kind node -- nothing to remove.${NC}"
else
  while IFS= read -r ref; do
    [[ -z "$ref" ]] && continue
    echo -e "${GREEN}▶   removing $ref${NC}"
    if ! docker exec "$KIND_CONTROL_PLANE" crictl rmi "$ref" >/dev/null 2>&1; then
      echo -e "${RED}▶   FAIL: could not remove $ref -- is a pod still using it?${NC}"
      echo -e "${RED}▶   Try: helm uninstall $RELEASE -n $NAMESPACE${NC}"
      exit 1
    fi
    sleep 2
  done <<< "$stale_refs"
fi

# ---------------------------------------------------------------------------
# Phase B -- build via IPP's `make image-kind` + verify exactly one IPP image
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase B -- make image-kind (KIND_CLUSTER_NAME=${KIND_CLUSTER_NAME})...${NC}"
sleep 2

# The Makefile's image-kind target: image-build-local ($(E2E_IMAGE)) + kind load.
# E2E_IMAGE = $(IMAGE):e2e = ghcr.io/llm-d/$(PROJECT_NAME):e2e by default.
( cd "$IPP_PATH" && make image-kind KIND_CLUSTER_NAME="$KIND_CLUSTER_NAME" )

echo
echo -e "${GREEN}▶ Verifying exactly one IPP image on the kind node...${NC}"
sleep 2
post_refs=$(docker exec "$KIND_CONTROL_PLANE" crictl images 2>/dev/null \
  | awk 'NR>1 && $1 ~ /llm-d-inference-payload-processor/ {print $1":"$2}' \
  | sort -u)
count=$(printf '%s\n' "$post_refs" | grep -c . || true)

if [[ "$count" -eq 0 ]]; then
  echo -e "${RED}▶ FAIL: no IPP image on the kind node after image-kind -- did the build fail silently?${NC}"
  exit 1
elif [[ "$count" -gt 1 ]]; then
  echo -e "${RED}▶ FAIL: more than one IPP image on the kind node after Phase A/B:${NC}"
  echo "$post_refs"
  exit 1
fi
echo -e "${GREEN}▶   $post_refs${NC}"

# The Makefile tags E2E_IMAGE = $(IMAGE):e2e. If the user overrode IPP_IMAGE_TAG,
# the chart won't find the image kubelet has. Fail loud rather than pull from ghcr.
expected="$IPP_IMAGE_REPO:$IPP_IMAGE_TAG"
if [[ "$post_refs" != "$expected" ]]; then
  echo -e "${RED}▶ FAIL: loaded image '$post_refs' does not match chart target '$expected'.${NC}"
  echo -e "${RED}▶ Either unset IPP_IMAGE_REPO/IPP_IMAGE_TAG (default: $IPP_IMAGE_REPO:e2e)${NC}"
  echo -e "${RED}▶ or re-run the Makefile with matching E2E_IMAGE/IMAGE/REGISTRY variables.${NC}"
  exit 1
fi

# ---------------------------------------------------------------------------
# Phase C -- render scorer values file (only if missing)
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase C -- render ${SCORER} values file (if missing)...${NC}"
sleep 2

# One authoritative values file: the selected scorer in the request pipeline,
# model-cost-extractor under datalayer.extractors. Both plugins configured
# with default parameters (no explicit `parameters:` blocks). No second file
# is needed -- Helm merges by replacing lists, so splitting them would only
# invite clobbering.
#
# The type/pluginRef string is scorer-specific:
#   costguard -> `costguard`
#   costaware -> `cost-scorer` (per IPP costaware/plugin.go const CostScorerType)

if [[ -f "$IPP_VALUES" ]]; then
  echo -e "${YELLOW}▶   $IPP_VALUES already exists -- reusing (delete it to regenerate).${NC}"
else
  mkdir -p "$(dirname "$IPP_VALUES")"
  # SCORER_PLUGIN is the on-the-wire `type:`/`pluginRef:` string. For SCORER=
  # costguard it is literally `costguard`; for SCORER=costaware it is
  # `cost-scorer` (the plugin is registered under that name -- the package
  # directory is called `costaware`, but the registered type is
  # `cost-scorer`).
  case "$SCORER" in
    costguard) SCORER_PLUGIN=costguard ;;
    costaware) SCORER_PLUGIN=cost-scorer ;;
  esac
  cat > "$IPP_VALUES" <<EOF
# Auto-generated by ipp_deploy.sh (SCORER=${SCORER}).
# ${SCORER_PLUGIN} scorer + model-cost-extractor (both with default parameters),
# wired for the kind-sim-multi stack.
# Delete this file to regenerate; edit it to customize.
payloadProcessor:
  listModels:
    - facebook/opt-125m
    - facebook/opt-350m
  # Runner verbosity. v=4 (= logutil.DEBUG) enables the DEBUG-gated
  # per-request cost-metadata log events emitted by the
  # request-cost-metadata extractor (IPP PR #269). Chart default is v=3;
  # anything below 4 silences the DEBUG stream. Kept at 4 under both
  # scorers so model-cost-extractor observations are visible in the pod
  # log for post-mortem analysis.
  flags:
    v: 4
  customConfig:
    plugins:
    - type: body-field-to-header
      parameters:
        fieldName: model
        headerName: X-Gateway-Model-Name
    - type: base-model-to-header
    - type: model-selector
    - type: model-group-name-filter
    - type: ${SCORER_PLUGIN}
    - type: max-score-picker
    - type: model-cost-extractor
    - type: model-config-datasource
      parameters:
        modelsPath: /config/models.json
    profiles:
    - name: default
      plugins:
        request:
        - pluginRef: model-selector
        - pluginRef: model-group-name-filter
        - pluginRef: ${SCORER_PLUGIN}
          weight: 1.0
        - pluginRef: max-score-picker
        - pluginRef: body-field-to-header
        - pluginRef: base-model-to-header
    datalayer:
      extractors:
      - pluginRef: model-cost-extractor
      datasources:
      - pluginRef: model-config-datasource
  # Rendered by the chart into /config/models.json inside the pod.
  # model-config-datasource reads this to populate the datastore's model
  # set; the two entries here mirror listModels above and the BaseModel
  # ConfigMaps applied in Phase D. Pricing values are placeholders --
  # edit for realistic per-token costs.
  models:
    models:
      - name: facebook/opt-125m
        pricing:
          inputPerMillion: 0.5
          outputPerMillion: 1.5
      - name: facebook/opt-350m
        pricing:
          inputPerMillion: 0.1
          outputPerMillion: 0.3
    groups:
      - name: fast
        models: [facebook/opt-125m, facebook/opt-350m]
EOF
  echo -e "${GREEN}▶   wrote $IPP_VALUES${NC}"
fi
sleep 2

# ---------------------------------------------------------------------------
# Phase D -- helm upgrade --install + BaseModel CRs
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase D -- helm upgrade --install ${RELEASE}...${NC}"
sleep 2

helm upgrade --install "$RELEASE" \
  "$IPP_PATH/config/charts/payload-processor/" \
  -n "$NAMESPACE" \
  -f "$IPP_VALUES" \
  --set provider.name=istio \
  --set payloadProcessor.image.registry="$IPP_IMAGE_REGISTRY" \
  --set payloadProcessor.image.repository="$IPP_IMAGE_NAME" \
  --set payloadProcessor.image.tag="$IPP_IMAGE_TAG" \
  --set payloadProcessor.image.pullPolicy=Never \
  --set provider.supportedEvents.requestBody=true \
  --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseBody=true \
  --set provider.messageTimeout=1200s \
  --set inferenceGateway.name="$GATEWAY_NAME"

echo
echo -e "${GREEN}▶   applying BaseModel CRs...${NC}"
sleep 2
kubectl apply -n "$NAMESPACE" \
  -f "$REPO_ROOT/ipp_benchmarking/ipp_configs/opt-125m-base-model.yaml" \
  -f "$REPO_ROOT/ipp_benchmarking/ipp_configs/opt-350m-base-model.yaml"

# ---------------------------------------------------------------------------
# Phase E -- wait for rollout + verify plugins loaded
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase E -- waiting for ${RELEASE} rollout + verifying plugins...${NC}"
sleep 2
kubectl rollout status "deploy/${RELEASE}" -n "$NAMESPACE" --timeout=300s

# Rolling restart if the values didn't change since last run (chart lacks a
# ConfigMap-checksum annotation, so a pure customConfig edit doesn't restart
# the pod on its own -- README documents this gotcha).
echo
echo -e "${GREEN}▶   rolling restart to pick up latest customConfig...${NC}"
sleep 2
kubectl rollout restart "deploy/${RELEASE}" -n "$NAMESPACE"
kubectl rollout status "deploy/${RELEASE}" -n "$NAMESPACE" --timeout=300s

# Wait for the rolling restart to fully settle: old ReplicaSet's pods
# gone, exactly one Running pod for the current revision. `rollout
# status` above only guarantees Ready-replica count against the desired
# count; the old RS's pods can still be Terminating for several seconds
# after that returns, and picking the "newest" pod during that window
# is a coin flip -- if we pick the terminating one, `kubectl logs`
# either returns empty or fails with "pod not found" a moment later.
echo -e "${GREEN}▶   waiting for old ReplicaSet pods to terminate...${NC}"
for _ in $(seq 1 30); do
  npods=$(kubectl get pod -n "$NAMESPACE" -l "app=${RELEASE}" \
    --field-selector=status.phase=Running -o name 2>/dev/null | wc -l | tr -d ' ')
  if [[ "$npods" == "1" ]]; then break; fi
  sleep 2
done

# The payload-processor chart labels its pods `app=<release>` (not the
# k8s conventional `app.kubernetes.io/name`). Selecting on that label
# is deterministic; do NOT fall back to a name-substring grep -- when
# two revisions overlap during a rolling restart, sort-by-name picks
# whichever pod name happens to sort last and can silently return the
# terminating old pod.
pod=$(kubectl get pod -n "$NAMESPACE" -l "app=${RELEASE}" \
  --field-selector=status.phase=Running \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
if [[ -z "$pod" ]]; then
  echo -e "${RED}▶ FAIL: no Running ${RELEASE} pod matches -l app=${RELEASE} in ns/${NAMESPACE}.${NC}"
  kubectl get pods -n "$NAMESPACE" -l "app=${RELEASE}" -o wide || true
  exit 1
fi
echo -e "${GREEN}▶   inspecting pod: $pod${NC}"

# Give the pod a moment to log the loaded configuration before we grep.
# Surface `kubectl logs` errors -- swallowing them with `|| true` turns a
# "pod vanished" or auth failure into a misleading "plugin(s) missing".
sleep 2
if ! logs=$(kubectl logs "$pod" -n "$NAMESPACE" 2>&1); then
  echo -e "${RED}▶ FAIL: kubectl logs $pod -n $NAMESPACE failed:${NC}"
  echo "$logs"
  exit 1
fi

# Scorer-specific plugin name (as registered in the IPP plugin registry).
case "$SCORER" in
  costguard) SCORER_PLUGIN=costguard ;;
  costaware) SCORER_PLUGIN=cost-scorer ;;
esac

missing=""
for plugin in "$SCORER_PLUGIN" "model-cost-extractor"; do
  if ! grep -q -- "$plugin" <<<"$logs"; then
    missing="$missing $plugin"
  fi
done
if [[ -n "$missing" ]]; then
  echo -e "${RED}▶ FAIL: pod logs do not mention plugin(s):$missing${NC}"
  echo -e "${RED}▶ Full logs:${NC}"
  echo "$logs"
  exit 1
fi
echo -e "${GREEN}▶   both plugins present in loaded config: ${SCORER_PLUGIN}, model-cost-extractor${NC}"

# ---------------------------------------------------------------------------
# Phase F -- banner
# ---------------------------------------------------------------------------
echo
deployed_image=$(kubectl get pod "$pod" -n "$NAMESPACE" \
  -o jsonpath='{.spec.containers[0].image}' 2>/dev/null || echo "<unknown>")

cat <<EOF

$(printf "${GREEN}▶ IPP deploy complete.${NC}\n")

  Scorer:           ${SCORER} (plugin type: ${SCORER_PLUGIN})
  Release:          ${RELEASE}
  Namespace:        ${NAMESPACE}
  Deployed image:   ${deployed_image}
  Values file:      ${IPP_VALUES}
  Gateway service:  ${GATEWAY_SVC} (ClusterIP, NodePort-exposed)
  Plugins verified: ${SCORER_PLUGIN}, model-cost-extractor

  Send a completion through the Gateway from the mac (IPP now injects
  the model header, so the client no longer needs X-Gateway-Base-Model-Name).
  The Gateway is NodePort, not exposed on the mac host, so port-forward first:

    # In one shell:
    kubectl port-forward -n ${NAMESPACE} svc/${GATEWAY_SVC} 8080:80

    # In another:
    curl -s -H 'Content-Type: application/json' \\
         http://localhost:8080/v1/completions \\
         -d '{"model":"facebook/opt-125m","prompt":"hi","max_tokens":256}'

  Re-run this script to rebuild + redeploy (stale IPP images are removed
  from the kind node first). Tear down with:

    helm uninstall ${RELEASE} -n ${NAMESPACE}

EOF

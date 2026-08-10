#!/opt/homebrew/bin/bash
#
# models_patch_kind.sh -- patch models.json in the payload-processor ConfigMap
# from an IPP helm values file, without re-running helm/rebuild.
#
# The model-config-datasource plugin (pkg/framework/plugins/datalayer/
# modelconfigcollector/plugin.go in llm-d-inference-payload-processor) watches
# /config/models.json via fsnotify and re-syncs pricing + group membership
# in-memory on every change. Because the chart mounts the ConfigMap at /config
# as a full-directory projection (no subPath), a kubectl patch of the
# models.json key alone is enough for the running pod to pick up new prices
# and group memberships. NO rollout restart is needed.
#
# What this script does NOT do (out of scope):
#   * customConfig / default-ipp-config.yaml edits (plugin list, pipeline
#     order) -- those are loaded once at startup. Re-run ipp_deploy.sh.
#   * listModels changes -- baked into deployment envs at chart-render time.
#   * OCP -- kind-only guardrails; the OCP flow is essentially the same but
#     without the `kind get clusters` check.
#
# Usage:
#   ./ipp_benchmarking/tools/models_patch_kind.sh
#
# Environment overrides (all optional):
#   IPP_VALUES         values file to render models.json from, default:
#                      $BUNDLE_DIR/ipp_configs/kind-costguard/costguard-kind-values.yaml
#                      (matches ipp_deploy.sh)
#   KIND_CLUSTER_NAME  kind cluster to target, default: ipp-e2e
#   NAMESPACE          k8s namespace, default: llmdbench
#   RELEASE            helm release name (informational only), default:
#                      payload-processor
#   CM_NAME            ConfigMap to patch, default: payload-processor (matches
#                      the chart's payloadProcessor.name default; the ConfigMap
#                      name is NOT derived from the helm release name)

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-ipp-e2e}"
NAMESPACE="${NAMESPACE:-llmdbench}"
RELEASE="${RELEASE:-payload-processor}"
CM_NAME="${CM_NAME:-payload-processor}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

show_help() {
  cat <<EOF
Usage: $0 [-h|--help]

Patch models.json in the payload-processor ConfigMap on a kind cluster from an
IPP helm values file. The IPP model-config-datasource plugin watches this file
via fsnotify and re-syncs pricing + groups in-memory, so no pod restart is
needed. customConfig / listModels edits are NOT handled by this script -- use
ipp_deploy.sh for those.

Optional environment (defaults in parentheses):
  IPP_VALUES         values file                    (ipp_configs/kind-costguard/costguard-kind-values.yaml)
  KIND_CLUSTER_NAME  kind cluster name              (ipp-e2e)
  NAMESPACE          k8s namespace                  (llmdbench)
  RELEASE            helm release (informational)   (payload-processor)
  CM_NAME            ConfigMap to patch             (payload-processor)

Flags:
  -h, --help         Show this help.
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

IPP_VALUES="${IPP_VALUES:-$BUNDLE_DIR/ipp_configs/kind-costguard/costguard-kind-values.yaml}"

for tool in yq jq kubectl kind; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo -e "${RED}▶ FAIL: '$tool' not on PATH.${NC}"
    case "$tool" in
      yq)      echo -e "${RED}▶ Install: brew install yq${NC}" ;;
      jq)      echo -e "${RED}▶ Install: brew install jq${NC}" ;;
      kubectl) echo -e "${RED}▶ Install: brew install kubectl${NC}" ;;
      kind)    echo -e "${RED}▶ Install: brew install kind${NC}" ;;
    esac
    exit 1
  fi
done

if [[ ! -f "$IPP_VALUES" ]]; then
  echo -e "${RED}▶ FAIL: IPP_VALUES not found: $IPP_VALUES${NC}"
  echo -e "${RED}▶ Run make ipp-deploy first to generate the default values file,${NC}"
  echo -e "${RED}▶ or point IPP_VALUES at an existing file.${NC}"
  exit 1
fi

if ! kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
  echo -e "${RED}▶ FAIL: kind cluster '${KIND_CLUSTER_NAME}' does not exist.${NC}"
  echo -e "${RED}▶ Run make bootstrap-colima first (or export KIND_CLUSTER_NAME=<yours>).${NC}"
  exit 1
fi

current_ctx=$(kubectl config current-context 2>/dev/null || true)
if [[ "$current_ctx" != "kind-${KIND_CLUSTER_NAME}" ]]; then
  echo -e "${YELLOW}▶ kubectl context is '${current_ctx}', switching to 'kind-${KIND_CLUSTER_NAME}'.${NC}"
  kubectl config use-context "kind-${KIND_CLUSTER_NAME}" >/dev/null
fi

if ! kubectl get cm "$CM_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo -e "${RED}▶ FAIL: ConfigMap '${CM_NAME}' not found in ns/${NAMESPACE}.${NC}"
  echo -e "${RED}▶ Is IPP installed? Run make ipp-deploy first.${NC}"
  exit 1
fi

pod=$(kubectl get pod -n "$NAMESPACE" -l "app=${RELEASE}" \
  --field-selector=status.phase=Running \
  --sort-by=.metadata.creationTimestamp \
  -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
if [[ -z "$pod" ]]; then
  echo -e "${RED}▶ FAIL: no Running pod matches -l app=${RELEASE} in ns/${NAMESPACE}.${NC}"
  echo -e "${RED}▶ Is IPP installed? Run make ipp-deploy first.${NC}"
  exit 1
fi

echo -e "${GREEN}▶   IPP_VALUES:         $IPP_VALUES${NC}"
echo -e "${GREEN}▶   KIND_CLUSTER_NAME:  $KIND_CLUSTER_NAME${NC}"
echo -e "${GREEN}▶   NAMESPACE:          $NAMESPACE${NC}"
echo -e "${GREEN}▶   CM_NAME:            $CM_NAME${NC}"
echo -e "${GREEN}▶   Running pod:        $pod${NC}"
sleep 2

# ---------------------------------------------------------------------------
# Phase A -- build models.json from the values file
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase A -- rendering models.json from IPP_VALUES...${NC}"
sleep 2

# Mirrors config/charts/payload-processor/templates/config.yaml (chart PR #277):
#   * camelCase in values file -> snake_case in models.json
#     (input_per_million / output_per_million; the plugin unmarshals via
#     pricing.ModelPriceShape, which expects snake_case)
#   * missing pricing sub-fields default to 0 (matches Helm's `default 0`)
#   * groups block omitted entirely when the values file has no groups
#     (matches the chart's `{{- if .groups }}` conditional)
#
# Split into two stages so the control-flow (`if...then...else`) lives in jq,
# which handles it cleanly -- yq (mikefarah) chokes on inline if/then/else.
NEW_JSON=$(yq -o=json '.payloadProcessor.models' "$IPP_VALUES" | jq '
{
  "models": [.models[] | {
    "name": .name,
    "pricing": {
      "input_per_million":  (.pricing.inputPerMillion // 0),
      "output_per_million": (.pricing.outputPerMillion // 0)
    }
  }]
} + (
  if (.groups | type == "array") and (.groups | length > 0) then
    {"groups": [.groups[] | {"name": .name, "models": .models}]}
  else
    {}
  end
)')

if [[ -z "$NEW_JSON" || "$NEW_JSON" == "null" ]]; then
  echo -e "${RED}▶ FAIL: could not extract payloadProcessor.models from $IPP_VALUES${NC}"
  exit 1
fi

CUR_JSON=$(kubectl get cm "$CM_NAME" -n "$NAMESPACE" \
  -o jsonpath='{.data.models\.json}' 2>/dev/null || echo "")

# Normalize both sides through jq so key-order/whitespace differences don't
# trigger a spurious patch.
CUR_NORM=$(printf '%s' "$CUR_JSON" | jq -S . 2>/dev/null || echo "")
NEW_NORM=$(printf '%s' "$NEW_JSON" | jq -S . 2>/dev/null || echo "")

if [[ "$CUR_NORM" == "$NEW_NORM" ]]; then
  echo -e "${GREEN}▶   models.json in cm/${CM_NAME} is already up to date -- nothing to patch.${NC}"
  exit 0
fi

echo -e "${GREEN}▶   diff (current -> new):${NC}"
diff <(printf '%s\n' "$CUR_NORM") <(printf '%s\n' "$NEW_NORM") || true

# ---------------------------------------------------------------------------
# Phase B -- patch the ConfigMap
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase B -- patching cm/${CM_NAME} in ns/${NAMESPACE}...${NC}"
sleep 2

PATCH_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# jq -n --arg avoids fragile shell-quoting when the JSON contains newlines and
# double quotes. --type=merge only touches the specified data key; other keys
# (default-ipp-config.yaml, custom-ipp-config.yaml) are untouched.
patch_body=$(jq -n --arg mj "$NEW_JSON" '{data: {"models.json": $mj}}')
kubectl patch cm "$CM_NAME" -n "$NAMESPACE" --type=merge -p "$patch_body"

# ---------------------------------------------------------------------------
# Phase C -- verify ConfigMap read-back + fsnotify pickup
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase C -- verifying patch landed...${NC}"
sleep 2

READ_BACK=$(kubectl get cm "$CM_NAME" -n "$NAMESPACE" \
  -o jsonpath='{.data.models\.json}' 2>/dev/null)
READ_BACK_NORM=$(printf '%s' "$READ_BACK" | jq -S . 2>/dev/null || echo "")

if [[ "$READ_BACK_NORM" != "$NEW_NORM" ]]; then
  echo -e "${RED}▶ FAIL: ConfigMap read-back does not match patched value.${NC}"
  echo -e "${RED}▶ Expected:${NC}"
  printf '%s\n' "$NEW_NORM"
  echo -e "${RED}▶ Got:${NC}"
  printf '%s\n' "$READ_BACK_NORM"
  exit 1
fi
echo -e "${GREEN}▶   cm/${CM_NAME} read-back matches -- patch landed on the API server.${NC}"

# Wait up to ~30s for a model-config-datasource log line after PATCH_TS,
# indicating the fsnotify path fired inside the pod. Kubelet's projected-volume
# resync can lag on kind (default ~60s worst-case), so a missing log line is a
# warning, not an error.
echo -e "${GREEN}▶   waiting up to 60s for model-config-datasource to re-sync...${NC}"
saw_resync=""
for _ in $(seq 1 30); do
  if kubectl logs "$pod" -n "$NAMESPACE" --since-time="$PATCH_TS" 2>/dev/null \
       | grep -q "model-config-datasource"; then
    saw_resync="yes"
    break
  fi
  sleep 2
done

if [[ -n "$saw_resync" ]]; then
  echo -e "${GREEN}▶   model-config-datasource re-sync detected in $pod logs.${NC}"
else
  echo -e "${YELLOW}▶   no model-config-datasource log line seen within 60s.${NC}"
  echo -e "${YELLOW}▶   Kubelet projected-volume resync may still be pending -- the${NC}"
  echo -e "${YELLOW}▶   change WILL land. Check with:${NC}"
  echo -e "${YELLOW}▶     kubectl logs $pod -n $NAMESPACE --since=2m | grep model-config-datasource${NC}"
  echo -e "${YELLOW}▶     kubectl exec $pod -n $NAMESPACE -- cat /config/models.json${NC}"
fi

# ---------------------------------------------------------------------------
# Phase D -- banner
# ---------------------------------------------------------------------------
echo
cat <<EOF

$(printf "${GREEN}▶ models.json patch complete.${NC}\n")

  Values file:       ${IPP_VALUES}
  ConfigMap:         cm/${CM_NAME} in ns/${NAMESPACE}
  Pod that saw it:   ${pod}
  Rollout restart:   NOT needed -- model-config-datasource re-syncs in-memory.

  Confirm the projected file inside the pod (once kubelet's resync lands,
  typically <60s on kind):

    kubectl exec ${pod} -n ${NAMESPACE} -- cat /config/models.json

  Confirm downstream cost accounting picks up the new prices:

    kubectl logs ${pod} -n ${NAMESPACE} --since=30s \\
      | grep 'request-cost-metadata observation' | jq

EOF

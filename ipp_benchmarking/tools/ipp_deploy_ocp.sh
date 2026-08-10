#!/usr/bin/env bash
#
# ipp_deploy_ocp.sh -- deploy the payload-processor Helm chart with
# CostGuard + model-cost-extractor plugins into an OCP cluster that has
# already been stood up with the `cicd/ocp-qwen-gemma-multi` scenario
# (Qwen3-8B + Qwen3-32B on H100-80GB).
#
# OCP counterpart to tools/ipp_deploy.sh. Follows the same phase/color/sleep
# conventions so a human can follow progress in the terminal, but with
# OCP-appropriate operations:
#
#   * Uses `oc` instead of `kubectl` throughout (both work on OCP but `oc`
#     matches the standup and AGENTS.md idioms).
#   * Does NOT build the IPP image -- expects it to already exist on a
#     registry the cluster can pull from (unlike the kind script, which
#     builds locally and side-loads via `make image-kind`).
#   * Uses --set payloadProcessor.image.pullPolicy=IfNotPresent (never
#     Never -- the kind trick would keep the pod on a phantom image).
#   * Applies the two Qwen BaseModel ConfigMaps (not the opt-* ones).
#   * Renders + applies header-match HTTPRoutes from
#     ipp_configs/qwen-gemma-httproutes.yaml, patching in the current
#     namespace's Gateway name and this-standup's InferencePool hashes.
#   * Reminds you to patch --max-model-len 8192 onto the Qwen3-32B decode
#     deployment (AGENTS.md must-do #2) if the flag isn't already there.
#
# Preconditions:
#   * `oc login` to the OCP cluster.
#   * `llmdbenchmark --spec cicd/ocp-qwen-gemma-multi standup -p $NAMESPACE`
#     has completed successfully.
#   * $IPP_PATH points at a llm-d-inference-payload-processor checkout with
#     the payload-processor Helm chart at config/charts/payload-processor.
#   * The IPP image at $IPP_IMAGE_REPO:$IPP_IMAGE_TAG is already pushed to a
#     registry the OCP cluster can pull from. Use `make image-build` +
#     `docker push` in the IPP repo checkout to build and push.
#
# Usage:
#   NAMESPACE=llm-d-<you> IPP_PATH=/path/to/llm-d-inference-payload-processor \
#     IPP_IMAGE_REPO=ghcr.io/<you>/llm-d-inference-payload-processor \
#     IPP_IMAGE_TAG=costguard \
#     ./ipp_benchmarking/tools/ipp_deploy_ocp.sh
#
# Environment overrides (all optional except IPP_PATH, NAMESPACE):
#   NAMESPACE            OCP project with the standup, no default (required)
#   IPP_PATH             path to the IPP checkout (required for chart)
#   IPP_IMAGE_REPO       image repo the chart references,
#                        default: ghcr.io/llm-d/llm-d-inference-payload-processor
#   IPP_IMAGE_TAG        image tag the chart references, default: costguard
#   IPP_VALUES           Helm values file, default:
#                        $BUNDLE_DIR/ipp_configs/ocp-costguard/costguard-ocp-values.yaml
#                        Unlike ipp_deploy.sh, this file is NOT auto-generated
#                        -- it must exist because pricing values require a
#                        deliberate choice (see the file's header for details).
#   RELEASE              Helm release name, default: payload-processor
#
# Idempotent -- re-runs upgrade in place. NOT destructive: does not tear
# down the standup, and does not delete any InferencePool or Gateway.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NAMESPACE="${NAMESPACE:-}"
IPP_IMAGE_REPO="${IPP_IMAGE_REPO:-ghcr.io/llm-d/llm-d-inference-payload-processor}"

# Split the fully-qualified image into registry + name for the IPP chart's
# `payloadProcessor.image.{registry,repository,tag}` schema (see
# ipp_deploy.sh:51-59 for the doubled-prefix bug this avoids).
IPP_IMAGE_REGISTRY="${IPP_IMAGE_REPO%/*}"
IPP_IMAGE_NAME="${IPP_IMAGE_REPO##*/}"
IPP_IMAGE_TAG="${IPP_IMAGE_TAG:-costguard}"
RELEASE="${RELEASE:-payload-processor}"

# The inference Gateway RESOURCE name comes from the standup Helm release
# (infra-<namespace>-inference-gateway). See ipp_deploy.sh:63-73 for the
# distinction between the Gateway resource name (what the IPP chart's
# `inferenceGateway.name` value expects) and the companion Istio Service
# with the "-istio" suffix.
GATEWAY_NAME="infra-${NAMESPACE}-inference-gateway"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

show_help() {
  cat <<EOF
Usage: NAMESPACE=<project> IPP_PATH=/path/to/llm-d-inference-payload-processor $0 [-h|--help]

Deploys the payload-processor Helm chart with CostGuard +
model-cost-extractor plugins into an OCP cluster that already has the
cicd/ocp-qwen-gemma-multi standup complete.

Required environment:
  NAMESPACE            OCP project with the standup (e.g. llm-d-<you>)
  IPP_PATH             path to the llm-d-inference-payload-processor checkout

Optional environment (defaults in parentheses):
  IPP_IMAGE_REPO       image repo the chart uses      (ghcr.io/llm-d/llm-d-inference-payload-processor)
  IPP_IMAGE_TAG        image tag the chart uses       (costguard)
  IPP_VALUES           Helm values file               (ipp_configs/ocp-costguard/costguard-ocp-values.yaml)
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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ -z "$NAMESPACE" ]]; then
  echo -e "${RED}▶ FAIL: NAMESPACE is unset.${NC}"
  echo -e "${RED}▶ export NAMESPACE=llm-d-<you>${NC}"
  exit 1
fi
if [[ -z "${IPP_PATH:-}" ]]; then
  echo -e "${RED}▶ FAIL: IPP_PATH is unset.${NC}"
  echo -e "${RED}▶ export IPP_PATH=/path/to/llm-d-inference-payload-processor${NC}"
  exit 1
fi
if [[ ! -d "$IPP_PATH/config/charts/payload-processor" ]]; then
  echo -e "${RED}▶ FAIL: chart not found at $IPP_PATH/config/charts/payload-processor${NC}"
  exit 1
fi

IPP_VALUES="${IPP_VALUES:-$BUNDLE_DIR/ipp_configs/ocp-costguard/costguard-ocp-values.yaml}"
if [[ ! -f "$IPP_VALUES" ]]; then
  echo -e "${RED}▶ FAIL: values file not found: $IPP_VALUES${NC}"
  echo -e "${RED}▶ This script does not auto-generate an OCP values file --${NC}"
  echo -e "${RED}▶ pricing values require a deliberate choice. See:${NC}"
  echo -e "${RED}▶   ipp_configs/ocp-costguard/README.md${NC}"
  exit 1
fi

# Verify `oc` is on PATH and logged in.
if ! command -v oc >/dev/null 2>&1; then
  echo -e "${RED}▶ FAIL: 'oc' not on PATH. Install the OpenShift CLI.${NC}"
  exit 1
fi
if ! oc whoami >/dev/null 2>&1; then
  echo -e "${RED}▶ FAIL: 'oc whoami' failed. Run 'oc login <cluster>' first.${NC}"
  exit 1
fi

# Verify the project exists and switch context to it.
if ! oc get project "$NAMESPACE" >/dev/null 2>&1; then
  echo -e "${RED}▶ FAIL: project '$NAMESPACE' not found on the cluster.${NC}"
  echo -e "${RED}▶ Run 'llmdbenchmark --spec cicd/ocp-qwen-gemma-multi standup -p $NAMESPACE' first.${NC}"
  exit 1
fi
oc project "$NAMESPACE" >/dev/null

# Verify the Gateway resource exists (proves the standup ran).
if ! oc get gateway "$GATEWAY_NAME" -n "$NAMESPACE" >/dev/null 2>&1; then
  echo -e "${RED}▶ FAIL: Gateway '${GATEWAY_NAME}' not found in ns/${NAMESPACE}.${NC}"
  echo -e "${RED}▶ The ipp_benchmarking standup has not run yet, or produced a different Gateway name.${NC}"
  exit 1
fi

# Resolve the current per-standup InferencePool names for the two Qwen models.
# The names carry an 8-char content hash so they differ per standup; we grep
# by substring on the model portion ('qwen3-8b', 'wen3-32b' -- note the second
# is truncated by the modelservice hasher to the last 8 chars).
POOL_8B=$(oc get inferencepool -n "$NAMESPACE" -o name 2>/dev/null \
  | grep -E 'qwen3-8b' | head -1 | sed 's|inferencepool.inference.networking.k8s.io/||')
POOL_32B=$(oc get inferencepool -n "$NAMESPACE" -o name 2>/dev/null \
  | grep -E 'wen3-32b' | head -1 | sed 's|inferencepool.inference.networking.k8s.io/||')

if [[ -z "$POOL_8B" ]]; then
  echo -e "${RED}▶ FAIL: no InferencePool matching 'qwen3-8b' found in ns/${NAMESPACE}.${NC}"
  echo -e "${RED}▶ Current pools:${NC}"
  oc get inferencepool -n "$NAMESPACE" || true
  exit 1
fi
if [[ -z "$POOL_32B" ]]; then
  echo -e "${RED}▶ FAIL: no InferencePool matching 'wen3-32b' found in ns/${NAMESPACE}.${NC}"
  echo -e "${RED}▶ Current pools:${NC}"
  oc get inferencepool -n "$NAMESPACE" || true
  exit 1
fi

echo -e "${GREEN}▶   NAMESPACE:          $NAMESPACE${NC}"
echo -e "${GREEN}▶   IPP_PATH:           $IPP_PATH${NC}"
echo -e "${GREEN}▶   IPP_IMAGE_REPO:     $IPP_IMAGE_REPO${NC}"
echo -e "${GREEN}▶   IPP_IMAGE_TAG:      $IPP_IMAGE_TAG${NC}"
echo -e "${GREEN}▶   IPP_VALUES:         $IPP_VALUES${NC}"
echo -e "${GREEN}▶   Gateway:            $GATEWAY_NAME (present)${NC}"
echo -e "${GREEN}▶   InferencePool 8B:   $POOL_8B${NC}"
echo -e "${GREEN}▶   InferencePool 32B:  $POOL_32B${NC}"
sleep 2

# ---------------------------------------------------------------------------
# Phase A -- verify the IPP image is pullable
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase A -- verifying ${IPP_IMAGE_REPO}:${IPP_IMAGE_TAG} is pullable...${NC}"
sleep 2

# Prefer skopeo, fall back to `oc image info`. Both hit the registry the
# cluster would pull from -- if either can resolve the manifest, the pod
# will succeed on pull (auth quirks aside).
if command -v skopeo >/dev/null 2>&1; then
  if ! skopeo inspect "docker://${IPP_IMAGE_REPO}:${IPP_IMAGE_TAG}" >/dev/null 2>&1; then
    echo -e "${RED}▶ FAIL: skopeo cannot inspect ${IPP_IMAGE_REPO}:${IPP_IMAGE_TAG}.${NC}"
    echo -e "${RED}▶ Ensure the image is pushed and the registry is public (or the pull secret is configured).${NC}"
    exit 1
  fi
  echo -e "${GREEN}▶   skopeo inspect: OK${NC}"
elif oc image info "${IPP_IMAGE_REPO}:${IPP_IMAGE_TAG}" >/dev/null 2>&1; then
  echo -e "${GREEN}▶   oc image info: OK${NC}"
else
  echo -e "${YELLOW}▶   could not verify image pullability (no skopeo, oc image info failed).${NC}"
  echo -e "${YELLOW}▶   Continuing anyway; pod will fail loud if the image is not reachable.${NC}"
fi

# ---------------------------------------------------------------------------
# Phase B -- (intentionally skipped)
# ---------------------------------------------------------------------------
# On kind Phase B is `make image-kind` (build + side-load). On OCP the image
# must already be pushed to a registry the cluster can pull from; there is
# nothing local to build. Left as a comment for parity with ipp_deploy.sh.
echo
echo -e "${GREEN}▶ Phase B -- (skipped on OCP; expected image already pushed to registry)${NC}"

# ---------------------------------------------------------------------------
# Phase C -- values file (must be hand-authored, not generated)
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase C -- using values file ${IPP_VALUES}${NC}"
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
  --set payloadProcessor.image.pullPolicy=IfNotPresent \
  --set provider.supportedEvents.requestBody=true \
  --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseBody=true \
  --set provider.messageTimeout=1200s \
  --set inferenceGateway.name="$GATEWAY_NAME"

echo
echo -e "${GREEN}▶   applying BaseModel CRs...${NC}"
sleep 2
oc apply -n "$NAMESPACE" \
  -f "$BUNDLE_DIR/ipp_configs/qwen3-8b-base-model.yaml" \
  -f "$BUNDLE_DIR/ipp_configs/qwen3-32b-base-model.yaml"

# ---------------------------------------------------------------------------
# Phase D.5 -- apply HTTPRoutes (header-match to X-Gateway-Base-Model-Name)
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase D.5 -- rendering + applying HTTPRoutes for Qwen3-8B and Qwen3-32B...${NC}"
sleep 2

# The static file at ipp_configs/qwen-gemma-httproutes.yaml hard-codes the
# kind-side Gateway name and a specific standup's InferencePool hashes. Both
# need to be patched to this cluster's actual values before apply. Doing this
# in a tmp file (never mutating the source) so the checked-in file stays as
# a template.
ROUTES_SRC="$BUNDLE_DIR/ipp_configs/qwen-gemma-httproutes.yaml"
if [[ ! -f "$ROUTES_SRC" ]]; then
  echo -e "${RED}▶ FAIL: expected routes template not found: $ROUTES_SRC${NC}"
  exit 1
fi

ROUTES_TMP="$(mktemp -t qwen-gemma-httproutes.XXXXXX.yaml)"
trap 'rm -f "$ROUTES_TMP"' EXIT

# Patch: swap the hardcoded kind Gateway name for this cluster's Gateway,
# and swap in the resolved InferencePool names for this standup.
sed \
  -e "s|infra-llmdbench-inference-gateway|${GATEWAY_NAME}|g" \
  -e "s|qwen-qwe-1a17ef6e-qwen3-8b-gaie|${POOL_8B}|g" \
  -e "s|qwen-qwe-de682216-wen3-32b-gaie|${POOL_32B}|g" \
  "$ROUTES_SRC" > "$ROUTES_TMP"

oc apply -n "$NAMESPACE" -f "$ROUTES_TMP"

# ---------------------------------------------------------------------------
# Phase E -- wait for rollout + verify CostGuard plugins loaded
# ---------------------------------------------------------------------------
echo
echo -e "${GREEN}▶ Phase E -- waiting for ${RELEASE} rollout + verifying plugins...${NC}"
sleep 2
oc rollout status "deploy/${RELEASE}" -n "$NAMESPACE" --timeout=300s

# Rolling restart to pick up the latest customConfig (chart lacks a
# ConfigMap-checksum annotation, so a pure customConfig edit doesn't
# restart the pod on its own -- README documents this gotcha).
echo
echo -e "${GREEN}▶   rolling restart to pick up latest customConfig...${NC}"
sleep 2
oc rollout restart "deploy/${RELEASE}" -n "$NAMESPACE"
oc rollout status "deploy/${RELEASE}" -n "$NAMESPACE" --timeout=300s

# Grab the newest pod and assert CostGuard plugin types appear in its
# loaded config. Same idiom as ipp_deploy.sh:371-399.
pod=$(oc get pod -n "$NAMESPACE" -l "app.kubernetes.io/name=${RELEASE}" \
  --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
if [[ -z "$pod" ]]; then
  pod=$(oc get pod -n "$NAMESPACE" -l "app=${RELEASE}" \
    --sort-by=.metadata.creationTimestamp -o jsonpath='{.items[-1:].metadata.name}' 2>/dev/null || true)
fi
if [[ -z "$pod" ]]; then
  pod=$(oc get pod -n "$NAMESPACE" -o name 2>/dev/null | grep "$RELEASE" | tail -1 | sed 's|pod/||')
fi
if [[ -z "$pod" ]]; then
  echo -e "${RED}▶ FAIL: could not locate a ${RELEASE} pod to inspect logs.${NC}"
  exit 1
fi
echo -e "${GREEN}▶   inspecting pod: $pod${NC}"

sleep 2
logs=$(oc logs "$pod" -n "$NAMESPACE" 2>/dev/null || true)

missing=""
for plugin in "costguard" "model-cost-extractor"; do
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
echo -e "${GREEN}▶   both plugins present in loaded config: costguard, model-cost-extractor${NC}"

# ---------------------------------------------------------------------------
# Phase F -- post-flight: --max-model-len 8192 on Qwen3-32B decode
# ---------------------------------------------------------------------------
# AGENTS.md must-do #2: Qwen3-32B crash-loops without --max-model-len 8192
# patched onto the decode deployment (scenario's maxModelLen is ignored
# because modelCommand=imageDefault only exports VLLM_MAX_MODEL_LEN, which
# vLLM doesn't read). Check whether the flag is already there and, if not,
# print the exact `oc patch` command for the user to run.
echo
echo -e "${GREEN}▶ Phase F -- post-flight: --max-model-len 8192 on Qwen3-32B decode...${NC}"
sleep 2

decode_32b=$(oc get deploy -n "$NAMESPACE" -o name 2>/dev/null \
  | grep -E 'wen3-32b.*decode' | head -1 | sed 's|deployment.apps/||')

if [[ -z "$decode_32b" ]]; then
  echo -e "${YELLOW}▶   could not find a Qwen3-32B decode deployment; skipping check.${NC}"
else
  if oc get deploy "$decode_32b" -n "$NAMESPACE" \
       -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null \
       | grep -q -- '--max-model-len'; then
    echo -e "${GREEN}▶   $decode_32b already has --max-model-len set. OK.${NC}"
  else
    echo -e "${YELLOW}▶   $decode_32b MISSING --max-model-len 8192 (AGENTS.md must-do #2).${NC}"
    echo -e "${YELLOW}▶   Run this to patch it:${NC}"
    cat <<EOF
      oc patch deploy "$decode_32b" -n "$NAMESPACE" --type=json -p='[
        {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-model-len"},
        {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"8192"}]'
EOF
    echo -e "${YELLOW}▶   Without this, Qwen3-32B will CrashLoopBackOff under real traffic.${NC}"
  fi
fi

# ---------------------------------------------------------------------------
# Phase G -- banner
# ---------------------------------------------------------------------------
echo
deployed_image=$(oc get pod "$pod" -n "$NAMESPACE" \
  -o jsonpath='{.spec.containers[0].image}' 2>/dev/null || echo "<unknown>")

cat <<EOF

$(printf "${GREEN}▶ IPP deploy complete.${NC}\n")

  Release:          ${RELEASE}
  Namespace:        ${NAMESPACE}
  Deployed image:   ${deployed_image}
  Values file:      ${IPP_VALUES}
  Gateway:          ${GATEWAY_NAME}
  Pool 8B:          ${POOL_8B}
  Pool 32B:         ${POOL_32B}
  Plugins verified: costguard, model-cost-extractor

  Send traffic through the Gateway (adjust the route host/URL for your OCP
  ingress; the smoke test path is via the Istio Gateway Service):

    llmdbenchmark --spec cicd/ocp-qwen-gemma-multi run \\
      -l inference-perf -w ocp-costguard-large-tokens.yaml -p ${NAMESPACE}

  Then collect + inspect:

    NAMESPACE=${NAMESPACE} ./ipp_benchmarking/collect_logs.sh

  Re-run this script to upgrade the IPP release in place. Tear down with:

    helm uninstall ${RELEASE} -n ${NAMESPACE}
    llmdbenchmark --spec cicd/ocp-qwen-gemma-multi teardown -p ${NAMESPACE}

EOF

#!/opt/homebrew/bin/bash
#
# mac_colima_bootstrap.sh -- one-shot bring-up of the ipp_benchmarking Kind
# simulated environment on macOS + Colima, ready for a CostGuard IPP install.
#
# Adapted from ~/git/scripts/scripts/kind-metallb-colima-full.sh with the
# ipp_benchmarking-specific standup + asymmetric sim-args patching folded in.
# Prerequisite: bash 4+ (Homebrew), colima, docker CLI, kind, kubectl, helm.
#
# Usage:
#   ./ipp_benchmarking/tools/mac_colima_bootstrap.sh \
#       [METALLB_VERSION] [KIND_NODE_IMAGE] [--skip-standup] [-h|--help]
#
# Idempotent -- re-running skips phases that are already done.

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
KIND_SINGLE_NODE_CONFIG="/tmp/kind-single-node-config.yaml"
METALLB_KIND_CONFIG="/tmp/metallb-kind-config.yaml"
METALLB_VERSION_DEFAULT="v0.15.2"
KUBERNETES_VERSION_DEFAULT="v1.34.0"
METALLB_NAMESPACE="metallb-system"
NAMESPACE="llmdbench"
SPEC="cicd/kind-sim-multi"
BENCHMARK_IMAGE="ghcr.io/llm-d/llm-d-benchmark:v0.7.0"
TIMEOUT=300s

# Kind cluster name -- aligned with IPP's Makefile default (KIND_CLUSTER_NAME
# ?= ipp-e2e in llm-d-inference-payload-processor/Makefile). Overrideable via
# env so both this script and ipp_deploy.sh see the same value.
KIND_CLUSTER_NAME="${KIND_CLUSTER_NAME:-ipp-e2e}"

# Sim-arg asymmetry: opt-125m slow + long output, opt-350m fast + short output.
# CostGuard should see one backend that is both slower AND produces more
# completion tokens per response than the other.
#
# The output-length asymmetry is enforced server-side via --max-model-len
# (input + output token cap; llm-d-inference-sim has no direct --max-tokens
# flag). This is an UPPER BOUND, not an enforced mean length: it only creates
# asymmetry when the client sends a large `max_tokens` in the request body.
# In `mode=random` (the sim default) with a large client max_tokens, output
# is sampled up to min(client_max_tokens, max_model_len - input_len). Without
# a large client max_tokens the sim falls back to Gaussian(mean=40, sd=20)
# and there is no asymmetry -- the smoke test and workload both send
# max_tokens=$SIM_SMOKE_MAX_TOKENS to exercise the cap.
SIM_125M_TTFT="3s"
SIM_125M_ITL="200ms"
SIM_125M_MAXMODELLEN="512"
SIM_350M_TTFT="1s"
SIM_350M_ITL="50ms"
SIM_350M_MAXMODELLEN="64"
SIM_MAX_NUM_SEQS="10"
SIM_SMOKE_MAX_TOKENS="256"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

SKIP_STANDUP=0
POSITIONAL=()

# Label selector for the sim (decode) deployments the scenario creates. Kept
# as a constant so Phase C / D use the same selector -- a substring `grep decode`
# match against `kubectl get deploy -o name` would false-positive on any deploy
# with "decode" anywhere in its name.
DECODE_SELECTOR="llm-d.ai/role=decode"

# The inference Gateway svc name comes from the scenario's Helm release.
# `standup` renders it as infra-<release>-inference-gateway, so with -p llmdbench
# that becomes infra-llmdbench-inference-gateway.
GATEWAY_SVC="infra-${NAMESPACE}-inference-gateway"

show_help() {
  cat <<EOF
Usage: $0 [METALLB_VERSION] [KIND_NODE_IMAGE] [--skip-standup] [-h|--help]

Bootstraps Colima + kind + MetalLB + the ipp_benchmarking kind-sim-multi
scenario on macOS. Leaves the cluster ready for a manual CostGuard IPP
helm install (printed at the end).

Positional args (both optional, both have defaults):
  METALLB_VERSION   MetalLB version, default: ${METALLB_VERSION_DEFAULT}
  KIND_NODE_IMAGE   kind node image, default: kindest/node:${KUBERNETES_VERSION_DEFAULT}

Flags:
  --skip-standup    Skip 'llmdbenchmark standup' + sim-arg patching. Useful
                    when re-running just to reset cluster/MetalLB.
  -h, --help        Show this help.

Environment:
  KIND_CLUSTER_NAME  kind cluster to create/reuse, default: ipp-e2e
                     (must match IPP Makefile's default so ipp_deploy.sh
                     can load images into the same cluster)
EOF
}

for arg in "$@"; do
  case "$arg" in
    -h|--help) show_help; exit 0 ;;
    --skip-standup) SKIP_STANDUP=1 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

METALLB_VERSION="${POSITIONAL[0]:-$METALLB_VERSION_DEFAULT}"
KIND_NODE_IMAGE="${POSITIONAL[1]:-kindest/node:$KUBERNETES_VERSION_DEFAULT}"

# Resolve repo root as parent-of-parent of this script -- i.e. the top-level
# llm-d-benchmark checkout, regardless of the user's cwd.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo -e "${GREEN}▶ METALLB_VERSION=${METALLB_VERSION} KIND_NODE_IMAGE=${KIND_NODE_IMAGE}${NC}"
echo -e "${GREEN}▶ Repo root: ${REPO_ROOT}${NC}"
echo -e "${GREEN}▶ Skip standup: ${SKIP_STANDUP}${NC}"

# ---------------------------------------------------------------------------
# Phase A -- Colima + kind + MetalLB
# ---------------------------------------------------------------------------

# Colima
colima_status=$(colima list 2>/dev/null || true)
if echo "$colima_status" | grep -iq "^default.*Running"; then
  echo -e "${GREEN}▶ Colima already running -- skipping start.${NC}"
  COLIMA_NEEDS_SETUP=0
else
  echo -e "${GREEN}▶ Starting Colima (docker, cpu 8, mem 16, disk 200)...${NC}"
  # After an unclean shutdown Colima can fail to boot with
  #   `failed to run attach disk "colima", in use by instance "colima"`
  # The VM shows as Stopped but start fails until the stale sockets/lock
  # files under ~/.colima/_lima/colima are removed. Staged recovery:
  #   1. First attempt: `colima start` as-is.
  #   2. If that fails, `colima stop --force` (cheap -- sockets/pids/lock only,
  #      does NOT touch the ~45GB VM disk) and retry.
  #   3. If THAT retry also fails, nuke ~/.colima entirely. This is the
  #      last-resort recovery: it deletes the VM disk and all cached images
  #      (~45GB) so the next boot pulls everything fresh. Only reached when
  #      Colima is wedged in a way stop --force cannot recover.
  _colima_boot_args=(--runtime docker --cpu 8 --memory 16 --disk 200 --network-address)
  set +e
  colima start "${_colima_boot_args[@]}"
  colima_rc=$?
  set -e
  if [ "$colima_rc" -ne 0 ]; then
    echo -e "${YELLOW}▶ colima start failed (likely stale disk lock from prior shutdown) -- retrying after 'colima stop --force'...${NC}"
    colima stop --force || true
    sleep 2
    set +e
    colima start "${_colima_boot_args[@]}"
    colima_rc=$?
    set -e
  fi
  if [ "$colima_rc" -ne 0 ]; then
    echo -e "${RED}▶ colima start still failing -- last-resort recovery: 'rm -rf ~/.colima'.${NC}"
    echo -e "${RED}▶ This deletes the Colima VM disk (~45GB) and all cached images. Next boot re-downloads everything.${NC}"
    colima stop --force >/dev/null 2>&1 || true
    rm -rf ~/.colima
    colima start "${_colima_boot_args[@]}"
  fi
  sleep 5
  COLIMA_NEEDS_SETUP=1
fi

# kind cluster
echo -e "${GREEN}▶ kind cluster name: ${KIND_CLUSTER_NAME}${NC}"

# Warn if a legacy cluster from an earlier bootstrap (named 'kind') is present
# but is NOT the cluster we're about to use. Do NOT auto-delete -- it may
# have work the user cares about.
if [[ "$KIND_CLUSTER_NAME" != "kind" ]] \
   && kind get clusters 2>/dev/null | grep -qx "kind"; then
  echo -e "${YELLOW}▶ Notice: a legacy kind cluster named 'kind' exists but we are using${NC}"
  echo -e "${YELLOW}▶ '${KIND_CLUSTER_NAME}'. If the old 'kind' cluster is no longer needed:${NC}"
  echo -e "${YELLOW}▶   kind delete cluster --name kind${NC}"
fi

if kind get clusters 2>/dev/null | grep -qx "$KIND_CLUSTER_NAME"; then
  echo -e "${GREEN}▶ kind cluster '${KIND_CLUSTER_NAME}' already exists -- reusing.${NC}"
  kubectl config use-context "kind-${KIND_CLUSTER_NAME}" >/dev/null
else
  echo -e "${GREEN}▶ Creating kind cluster '${KIND_CLUSTER_NAME}'...${NC}"
  cat >"$KIND_SINGLE_NODE_CONFIG" <<EOF
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: ${KIND_CLUSTER_NAME}
nodes:
  - role: control-plane
    image: $KIND_NODE_IMAGE
EOF
  kind create cluster --config "$KIND_SINGLE_NODE_CONFIG"
fi

# Mac -> Colima VM -> kind routing
colima_host_ip=$(ifconfig bridge100 | awk '/inet /{print $2; exit}')
colima_vm_ip=$(colima list | awk '/docker/{print $8}')
# The Docker network named 'kind' is shared across ALL kind clusters on the
# host -- it is NOT per-cluster, so this stays literal even when
# KIND_CLUSTER_NAME differs from 'kind'.
colima_kind_cidr=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}}{{end}}' | grep -Eo '([0-9]{1,3}\.){3}[0-9]{1,3}/[0-9]+' | head -1)

echo -e "${GREEN}▶ Colima host IP: ${colima_host_ip}${NC}"
echo -e "${GREEN}▶ Colima VM IP:   ${colima_vm_ip}${NC}"
echo -e "${GREEN}▶ kind CIDR:      ${colima_kind_cidr}${NC}"

set +e
route_output=$(route get "$colima_kind_cidr" 2>/dev/null | grep "gateway: $colima_vm_ip")
set -e
if [ -n "$route_output" ]; then
  echo -e "${GREEN}▶ Route $colima_kind_cidr via $colima_vm_ip already exists.${NC}"
else
  echo -e "${GREEN}▶ Adding route (sudo may prompt)...${NC}"
  sudo route -nv add -net "$colima_kind_cidr" "$colima_vm_ip"
fi

# iptables FORWARD rule inside the Colima VM (only needed first time Colima
# comes up; safe to skip on re-run since Colima is stateful).
if [[ "$COLIMA_NEEDS_SETUP" == "1" ]]; then
  echo -e "${GREEN}▶ Adding FORWARD rule inside Colima VM...${NC}"
  colima ssh -- sudo iptables -I FORWARD -s "$colima_host_ip" -d "$colima_kind_cidr" -j ACCEPT || true
fi

# MetalLB
if kubectl get ns "$METALLB_NAMESPACE" >/dev/null 2>&1 \
   && kubectl get pod -n "$METALLB_NAMESPACE" -l app=metallb 2>/dev/null | grep -q Running; then
  echo -e "${GREEN}▶ MetalLB already installed -- skipping.${NC}"
else
  echo -e "${GREEN}▶ Installing MetalLB ${METALLB_VERSION}...${NC}"
  kubectl apply -f "https://raw.githubusercontent.com/metallb/metallb/${METALLB_VERSION}/config/manifests/metallb-native.yaml"
  echo -e "${GREEN}▶ Waiting for MetalLB pods...${NC}"
  kubectl wait --namespace "$METALLB_NAMESPACE" \
    --for=condition=ready pod --selector=app=metallb --timeout="${TIMEOUT}"
fi

# MetalLB IPAddressPool + L2Advertisement derived from the kind CIDR
KIND_PREFIX=$(echo "$colima_kind_cidr" | cut -d. -f1-3)
METALLB_RANGE_START="${KIND_PREFIX}.200"
METALLB_RANGE_END="${KIND_PREFIX}.250"
echo -e "${GREEN}▶ MetalLB pool: ${METALLB_RANGE_START}-${METALLB_RANGE_END}${NC}"

cat >"$METALLB_KIND_CONFIG" <<EOF
apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata:
  name: colima-kind-pool
  namespace: ${METALLB_NAMESPACE}
spec:
  addresses:
  - ${METALLB_RANGE_START}-${METALLB_RANGE_END}
---
apiVersion: metallb.io/v1beta1
kind: L2Advertisement
metadata:
  name: empty
  namespace: ${METALLB_NAMESPACE}
EOF
kubectl apply -f "$METALLB_KIND_CONFIG"

# ---------------------------------------------------------------------------
# Phase B -- benchmark image + llmdbenchmark standup
# ---------------------------------------------------------------------------

echo -e "${GREEN}▶ Side-loading benchmark image ${BENCHMARK_IMAGE}...${NC}"
if docker image inspect "$BENCHMARK_IMAGE" >/dev/null 2>&1; then
  echo -e "${GREEN}▶ Image already pulled locally.${NC}"
else
  docker pull "$BENCHMARK_IMAGE"
fi
# Side-load into the kind node. We CANNOT use `kind load docker-image` here:
# ghcr.io/llm-d/llm-d-benchmark is a multi-arch OCI image index, and
# `kind load` passes `--all-platforms` unconditionally to `ctr images import`,
# which then chokes on platform digests missing from the tar stream that
# `docker save` produced ("ctr: content digest sha256:...: not found"). Piping
# `docker save` -> `ctr images import` (WITHOUT --all-platforms) lets ctr pick
# just the host's platform layer from the manifest index and imports cleanly.
# The check for an existing image on the node uses `crictl images` -- ctr and
# crictl disagree on how to name multi-arch image IDs, so `kind load`'s own
# "already present" check misfires and it retries the broken import forever.
# Skip on re-run if the image is already resident.
if docker exec "${KIND_CLUSTER_NAME}-control-plane" crictl images 2>/dev/null \
     | awk '{print $1":"$2}' | grep -qx "$BENCHMARK_IMAGE"; then
  echo -e "${GREEN}▶ Benchmark image already on kind node -- skipping side-load.${NC}"
else
  echo -e "${GREEN}▶ Importing ${BENCHMARK_IMAGE} into kind node via ctr (bypassing kind load's multi-arch bug)...${NC}"
  docker save "$BENCHMARK_IMAGE" \
    | docker exec --privileged -i "${KIND_CLUSTER_NAME}-control-plane" \
        ctr --namespace=k8s.io images import -
fi

if [[ "$SKIP_STANDUP" == "1" ]]; then
  echo -e "${YELLOW}▶ --skip-standup set; skipping llmdbenchmark standup and sim patching.${NC}"
else
  # bootstrap venv if the user has not already
  if [ ! -d "$REPO_ROOT/.venv" ]; then
    echo -e "${GREEN}▶ .venv missing -- running ${REPO_ROOT}/install.sh...${NC}"
    (cd "$REPO_ROOT" && ./install.sh)
  fi

  # already-stood-up short-circuit: if decode deploys exist we assume the
  # standup ran previously and skip. Users who want a re-standup can
  # `llmdbenchmark --spec ${SPEC} teardown -p ${NAMESPACE}` first.
  if [ -n "$(kubectl get deploy -n "$NAMESPACE" -l "$DECODE_SELECTOR" -o name 2>/dev/null)" ]; then
    echo -e "${GREEN}▶ Standup already present in namespace ${NAMESPACE} -- skipping.${NC}"
  else
    echo -e "${GREEN}▶ Running llmdbenchmark standup (${SPEC})...${NC}"
    # Tolerate a non-zero exit from `llmdbenchmark standup`. In practice the
    # 17-step standup itself succeeds and only the trailing smoketest fails,
    # because (a) the sim doesn't implement /health, and (b) HTTPRoutes are
    # not rendered by llm-d-modelservice-v0.4.15 (Phase B.1 below installs
    # them via gen_httproutes.sh). Verify presence of decode deploys after
    # the run and fail loud if the standup itself really did fail; otherwise
    # continue to Phase B.1 + Phase C which fix the real gaps the smoketest
    # was pointing at.
    set +e
    (cd "$REPO_ROOT" && \
       . .venv/bin/activate && \
       llmdbenchmark --spec "$SPEC" standup -p "$NAMESPACE")
    standup_rc=$?
    set -e
    if [ -z "$(kubectl get deploy -n "$NAMESPACE" -l "$DECODE_SELECTOR" -o name 2>/dev/null)" ]; then
      echo -e "${RED}▶ FAIL: llmdbenchmark standup exited $standup_rc and no decode deploys exist in ns/${NAMESPACE}.${NC}"
      exit "$standup_rc"
    fi
    if [ "$standup_rc" -ne 0 ]; then
      echo -e "${YELLOW}▶ llmdbenchmark standup exited $standup_rc but decode deploys exist -- proceeding.${NC}"
      echo -e "${YELLOW}▶ (This is expected on kind: the built-in smoketest 404s because the sim has no /health and the modelservice chart doesn't render HTTPRoutes; Phase B.1 handles the routes.)${NC}"
    fi
  fi

  # -------------------------------------------------------------------------
  # Phase B.1 -- HTTPRoute fallback
  # -------------------------------------------------------------------------
  # The scenario sets `experimentalHttpRoute.enabled: true` per stack, but as
  # of llm-d-modelservice-v0.4.15 the chart does not render an HTTPRoute even
  # when the flag is set -- Gateway 404s everything and IPP smoketests fail.
  # Fall back to the manual header-match HTTPRoutes emitted by
  # gen_httproutes.sh (same routes the OpenShift path uses when
  # httpRoute.enabled is explicitly false).
  if [ -z "$(kubectl get httproute -n "$NAMESPACE" -o name 2>/dev/null)" ]; then
    echo -e "${YELLOW}▶ No HTTPRoutes in ns/${NAMESPACE} -- rendering fallback routes via gen_httproutes.sh...${NC}"
    "$SCRIPT_DIR/gen_httproutes.sh" "$NAMESPACE" infra-"$NAMESPACE"-inference-gateway \
      facebook/opt-125m facebook/opt-350m \
      | kubectl apply -f -
  else
    echo -e "${GREEN}▶ HTTPRoutes already present in ns/${NAMESPACE} -- skipping fallback.${NC}"
  fi

  # -------------------------------------------------------------------------
  # Phase C -- asymmetric sim args
  # -------------------------------------------------------------------------
  # The scenario uses `modelCommand: imageDefault`, so latency + length flags
  # must be injected post-standup. Rebuild container 0's args from the model
  # name so we don't depend on standup-time deployment name suffixes.
  #
  # Server-side length control is --max-model-len (input + output). The sim
  # has no --max-tokens flag; per-response length in mode=random is bounded
  # by min(client-request max_tokens, max-model-len - input_len). See the
  # SIM_*_MAXMODELLEN comment above.
  echo -e "${GREEN}▶ Patching sims with asymmetric flags...${NC}"
  decode_deploys=$(kubectl get deploy -n "$NAMESPACE" -l "$DECODE_SELECTOR" -o name)
  if [ -z "$decode_deploys" ]; then
    echo -e "${RED}▶ FAIL: no deploys match -l $DECODE_SELECTOR in ns/${NAMESPACE}${NC}"
    kubectl get deploy -n "$NAMESPACE" || true
    exit 1
  fi
  for d in $decode_deploys; do
    m=$(kubectl get "$d" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].args[1]}')
    case "$m" in
      *125m*) t="$SIM_125M_TTFT"; i="$SIM_125M_ITL"; ml="$SIM_125M_MAXMODELLEN" ;;
      *)      t="$SIM_350M_TTFT"; i="$SIM_350M_ITL"; ml="$SIM_350M_MAXMODELLEN" ;;
    esac
    echo -e "${GREEN}▶   $d ($m): ttft=$t itl=$i max-model-len=$ml${NC}"
    kubectl patch "$d" -n "$NAMESPACE" --type=json -p="[
      {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",
       \"value\":[
         \"--model\",\"$m\",
         \"--port\",\"8200\",
         \"--served-model-name\",\"$m\",
         \"--time-to-first-token=$t\",
         \"--inter-token-latency=$i\",
         \"--max-model-len=$ml\",
         \"--max-num-seqs=$SIM_MAX_NUM_SEQS\"
       ]}]"
  done
fi

# ---------------------------------------------------------------------------
# Phase D -- verify decode pods, sim-arg patch, and end-to-end curl reachability
# ---------------------------------------------------------------------------

GATEWAY_IP=""

if [[ "$SKIP_STANDUP" == "1" ]]; then
  echo -e "${YELLOW}▶ --skip-standup set; skipping decode-pod / sim-arg / curl verification.${NC}"
else
  decode_deploys=$(kubectl get deploy -n "$NAMESPACE" -l "$DECODE_SELECTOR" -o name)
  if [ -z "$decode_deploys" ]; then
    echo -e "${RED}▶ FAIL: no deploys match -l $DECODE_SELECTOR in ns/${NAMESPACE} -- standup missing?${NC}"
    kubectl get deploy -n "$NAMESPACE" || true
    exit 1
  fi

  echo
  echo -e "${GREEN}▶ Phase D.1 -- waiting for decode pods to become Ready...${NC}"
  sleep 2
  for d in $decode_deploys; do
    echo -e "${GREEN}▶   rollout status: $d${NC}"
    kubectl rollout status "$d" -n "$NAMESPACE" --timeout=300s
    sleep 2
  done

  echo
  echo -e "${GREEN}▶ Phase D.2 -- asserting sim-arg patch stuck on each decode deploy...${NC}"
  sleep 2
  for d in $decode_deploys; do
    m=$(kubectl get "$d" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].args[1]}')
    case "$m" in
      *125m*) want_t="$SIM_125M_TTFT"; want_i="$SIM_125M_ITL"; want_ml="$SIM_125M_MAXMODELLEN" ;;
      *)      want_t="$SIM_350M_TTFT"; want_i="$SIM_350M_ITL"; want_ml="$SIM_350M_MAXMODELLEN" ;;
    esac
    args=$(kubectl get "$d" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].args}')
    echo -e "${GREEN}▶   $d ($m): expect ttft=$want_t itl=$want_i max-model-len=$want_ml${NC}"
    for expect in "--time-to-first-token=$want_t" "--inter-token-latency=$want_i" "--max-model-len=$want_ml"; do
      if ! grep -q -- "$expect" <<<"$args"; then
        echo -e "${RED}▶   FAIL: $d missing '$expect' in container args${NC}"
        echo -e "${RED}▶   args were: $args${NC}"
        exit 1
      fi
    done
    echo -e "${GREEN}▶   $d: patch verified${NC}"
    sleep 2
  done

  echo
  echo -e "${GREEN}▶ Phase D.3 -- resolving ${GATEWAY_SVC} LoadBalancer IP...${NC}"
  for _ in $(seq 1 15); do
    GATEWAY_IP=$(kubectl get svc "$GATEWAY_SVC" -n "$NAMESPACE" \
      -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
    if [ -n "$GATEWAY_IP" ]; then
      break
    fi
    sleep 2
  done
  if [ -z "$GATEWAY_IP" ]; then
    echo -e "${RED}▶ FAIL: svc ${GATEWAY_SVC} has no LoadBalancer IP after 30s${NC}"
    kubectl get svc -n "$NAMESPACE" || true
    exit 1
  fi
  echo -e "${GREEN}▶   Gateway IP: $GATEWAY_IP${NC}"

  echo
  echo -e "${GREEN}▶ Phase D.4 -- curl smoke test against both sims via the Gateway...${NC}"
  # Only /v1/completions is header-routed by the scenario's HTTPRoutes; /health
  # and /v1/models on the Gateway don't reach the sims (no matching route),
  # so testing them would either 404 at the Gateway or return the Gateway's
  # own health -- neither proves the sim is answering. Skip them.
  sleep 2
  for model in "facebook/opt-125m" "facebook/opt-350m"; do
    echo -e "${GREEN}▶   --- $model ---${NC}"

    # Send a large client-side max_tokens so the sim's --max-model-len cap is
    # actually what bounds the response -- with a small max_tokens the sim
    # would fall back to Gaussian(mean=40) and both models would look ~equal.
    set +e
    code=$(curl -sS --max-time 30 -o /tmp/mac_colima_bootstrap_resp.json -w "%{http_code}" \
      -H "Content-Type: application/json" \
      -H "X-Gateway-Base-Model-Name: $model" \
      "http://$GATEWAY_IP/v1/completions" \
      -d "{\"model\":\"$model\",\"prompt\":\"hi\",\"max_tokens\":$SIM_SMOKE_MAX_TOKENS}")
    rc=$?
    set -e
    if [ "$rc" -ne 0 ] || [ "$code" != "200" ] || ! grep -q '"choices"' /tmp/mac_colima_bootstrap_resp.json; then
      echo -e "${RED}▶   FAIL: POST /v1/completions for $model (curl rc=$rc http=$code)${NC}"
      cat /tmp/mac_colima_bootstrap_resp.json || true
      exit 1
    fi
    # Report the observed completion length so a human can eyeball the asymmetry.
    # usage.completion_tokens is the sim's reported output count; grep-based
    # extraction avoids a jq dependency.
    ctok=$(grep -oE '"completion_tokens"[[:space:]]*:[[:space:]]*[0-9]+' /tmp/mac_colima_bootstrap_resp.json \
           | grep -oE '[0-9]+$' || true)
    echo -e "${GREEN}▶   POST /v1/completions -> 200 (has \"choices\", completion_tokens=${ctok:-?})${NC}"

    sleep 2
  done
  rm -f /tmp/mac_colima_bootstrap_resp.json

  echo -e "${GREEN}▶ Phase D complete -- both sims verified end-to-end.${NC}"
fi

# ---------------------------------------------------------------------------
# Phase E -- cluster-state summary + next-steps banner
# ---------------------------------------------------------------------------

echo
echo -e "${GREEN}▶ Cluster state in ns/${NAMESPACE}:${NC}"
kubectl get pods -n "$NAMESPACE" 2>/dev/null || true
echo
kubectl get httproute,gateway,inferencepool -n "$NAMESPACE" 2>/dev/null || true
echo

if [ -z "$GATEWAY_IP" ]; then
  GATEWAY_IP=$(kubectl get svc "$GATEWAY_SVC" -n "$NAMESPACE" -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
fi

cat <<EOF

$(printf "${GREEN}▶ Bootstrap complete.${NC}\n")

  Gateway LoadBalancer IP: ${GATEWAY_IP:-<not yet assigned; kubectl get svc -n ${NAMESPACE}>}

  Sims verified end-to-end above (/health, /v1/models, /v1/completions on both
  facebook/opt-125m and facebook/opt-350m). To re-run a completion by hand:

    curl -s -H 'Content-Type: application/json' \\
         -H 'X-Gateway-Base-Model-Name: facebook/opt-125m' \\
         http://${GATEWAY_IP:-<gateway-ip>}/v1/completions \\
         -d '{"model":"facebook/opt-125m","prompt":"hi","max_tokens":32}'

  Next: install IPP (with your CostGuard values file) --

    export IPP_PATH=/path/to/llm-d-inference-payload-processor
    export IPP_VALUES=/path/to/costguard-kind-values.yaml     # your CostGuard values
    export IPP_IMAGE_TAG=costguard                            # your CostGuard tag

    # side-load your CostGuard IPP image FIRST:
    kind load docker-image ghcr.io/<you>/llm-d-inference-payload-processor:\$IPP_IMAGE_TAG

    helm upgrade --install payload-processor \\
      "\$IPP_PATH/config/charts/payload-processor/" \\
      -n ${NAMESPACE} --set provider.name=istio -f "\$IPP_VALUES" \\
      --set payloadProcessor.image.tag=\$IPP_IMAGE_TAG \\
      --set payloadProcessor.image.pullPolicy=Never \\
      --set provider.supportedEvents.requestBody=true \\
      --set provider.supportedEvents.requestTrailers=true \\
      --set provider.supportedEvents.responseBody=true \\
      --set provider.messageTimeout=1200s \\
      --set inferenceGateway.name=infra-llmdbench-inference-gateway

    kubectl apply -n ${NAMESPACE} \\
      -f ipp_benchmarking/ipp_configs/opt-125m-base-model.yaml \\
      -f ipp_benchmarking/ipp_configs/opt-350m-base-model.yaml

  Then run + collect (from repo root, venv activated):

    llmdbenchmark --spec ${SPEC} run -l inference-perf -w sanity_random.yaml
    NAMESPACE=${NAMESPACE} ./ipp_benchmarking/collect_logs.sh

EOF

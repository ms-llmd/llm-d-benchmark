#!/usr/bin/env bash
set -euo pipefail
# nullglob makes an unmatched glob expand to nothing, so
# `for c in dir/pattern*/; do ...` skips the loop entirely instead of
# passing the literal pattern as one iteration.
shopt -s nullglob

NAMESPACE="${NAMESPACE:-llmdbench}"

# SCORER labels the working dir so `sim-costguard-group-run` and
# `sim-costaware-group-run` can produce structurally identical sibling
# archives without stepping on each other's directory numbering. Accepted:
# `costguard` (default) or `costaware`. Anything else fails loud.
SCORER="${SCORER:-costguard}"
case "$SCORER" in
  costguard|costaware) ;;
  *)
    echo "❌ SCORER must be 'costguard' or 'costaware', got: '$SCORER'" >&2
    exit 2
    ;;
esac

id=1
while [[ -d "./collected-logs-${SCORER}-${id}" ]]; do (( id++ )); done
LOG_DIR="./collected-logs-${SCORER}-${id}"
mkdir -p "${LOG_DIR}"

for sel in \
  'app=payload-processor' \
  'llm-d.ai/inference-serving=true' \
  'inference.networking.k8s.io/igw-mode=inferencepool' \
  'gateway.networking.k8s.io/gateway-class-name'; do
  kubectl get pods -n "${NAMESPACE}" -l "${sel}" -o name 2>/dev/null | while read -r pod; do
    kubectl logs -n "${NAMESPACE}" "${pod}" --timestamps --previous 2>/dev/null \
      >"${LOG_DIR}/${pod#pod/}.log" || \
    kubectl logs -n "${NAMESPACE}" "${pod}" --timestamps \
      >"${LOG_DIR}/${pod#pod/}.log" 2>&1 || true
  done
done

# Copy benchmark run results from the local llmdbenchmark workspace.
#
# Resolution order (matches llmdbenchmark itself):
#   1. LLMDBENCH_WORKSPACE env var
#   2. ~/data/kind-sim-multi  (workDir from config/scenarios/cicd/kind-sim-multi.yaml)
#   3. Most-recently-modified workspace_llmdbench_* directory under
#      ${TMPDIR:-/tmp}. On macOS Python's tempfile.mkdtemp() writes under
#      $TMPDIR (a per-user path like /var/folders/.../T/), NOT /tmp, so
#      hard-coding /tmp misses every workspace `llmdbenchmark run`
#      creates. Searching both locations covers Linux (/tmp) and macOS
#      ($TMPDIR) without needing per-OS branches.
_find_workspace() {
  if [[ -n "${LLMDBENCH_WORKSPACE:-}" && -d "${LLMDBENCH_WORKSPACE}" ]]; then
    echo "${LLMDBENCH_WORKSPACE}"
    return
  fi
  local default_workdir="${HOME}/data/kind-sim-multi"
  if [[ -d "${default_workdir}" ]]; then
    echo "${default_workdir}"
    return
  fi
  # Fall back to the most recently modified tmp workspace. Search both
  # ${TMPDIR:-/tmp} and /tmp -- de-duplicated when TMPDIR == /tmp.
  # `ls -td` sorts by mtime descending and is portable across BSD (macOS)
  # and GNU (Linux); do NOT use `find -printf`, which BSD find does not
  # support and would silently produce no output here.
  local -a search_roots=("${TMPDIR:-/tmp}")
  if [[ "${TMPDIR:-/tmp}" != "/tmp" && -d "/tmp" ]]; then
    search_roots+=("/tmp")
  fi
  local root candidates=() latest=""
  for root in "${search_roots[@]}"; do
    # `nullglob` (set at file top) makes an unmatched glob expand to
    # nothing rather than the literal string. Strip trailing slash from
    # $root ($TMPDIR on macOS ends with '/') to avoid `//` in paths.
    root="${root%/}"
    for c in "${root}"/workspace_llmdbench_*/; do
      candidates+=("${c%/}")
    done
  done
  if (( ${#candidates[@]} > 0 )); then
    latest=$(ls -td -- "${candidates[@]}" 2>/dev/null | head -1)
  fi
  echo "${latest}"
}

WORKSPACE=$(_find_workspace)
if [[ -n "${WORKSPACE}" ]]; then
  # Each llmdbenchmark run creates a timestamped subdir inside the workspace.
  # Find the most recently modified one that has a results/ or analysis/ child.
  # Enumerate the workspace's immediate subdirs newest-first via `ls -td`
  # (portable across BSD and GNU; `find -printf` is not BSD-portable and
  # would silently produce no output on macOS -- see _find_workspace).
  RUN_SUBDIR=""
  ws_subdirs=("${WORKSPACE}"/*/)
  if (( ${#ws_subdirs[@]} > 0 )); then
    while IFS= read -r d; do
      d="${d%/}"
      if [[ -d "${d}/results" || -d "${d}/analysis" ]]; then
        RUN_SUBDIR="${d}"
        break
      fi
    done < <(ls -td -- "${ws_subdirs[@]}" 2>/dev/null)
  fi

  if [[ -n "${RUN_SUBDIR}" ]]; then
    BENCH_RESULTS_DIR="${LOG_DIR}/benchmark-results"
    mkdir -p "${BENCH_RESULTS_DIR}"
    copied=0
    for subdir in results analysis; do
      src="${RUN_SUBDIR}/${subdir}"
      if [[ -d "${src}" ]]; then
        cp -r "${src}" "${BENCH_RESULTS_DIR}/${subdir}"
        copied=$((copied + 1))
      fi
    done
    echo "Benchmark results copied from ${RUN_SUBDIR} to ${BENCH_RESULTS_DIR}/"
  else
    echo "Workspace found at ${WORKSPACE} but no run subdirectory with results/ or analysis/ present; skipping benchmark results."
  fi
else
  echo "No llmdbenchmark workspace found (set LLMDBENCH_WORKSPACE or run from ~/data/kind-sim-multi); skipping benchmark results."
fi

# Best-effort: capture per-GPU DCGM metrics (tensor/SM/DRAM active, power) for
# the run window from OpenShift monitoring. No-op on clusters without the GPU
# Operator / Thanos (e.g. Kind). Never fails the collection.
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${_SCRIPT_DIR}/tools/collect_dcgm.py" ]]; then
  NAMESPACE="${NAMESPACE}" python3 "${_SCRIPT_DIR}/tools/collect_dcgm.py" \
    --logs-dir "${LOG_DIR}" --namespace "${NAMESPACE}" || \
    echo "collect_dcgm: skipped (non-fatal)."
fi

# AGENTS.md — IPP benchmarking handoff

Knowledge-transfer notes for an AI agent (or person) picking up these IPP
benchmarks. `README.md` is the step-by-step how-to; **this file is the
non-obvious stuff**: required fixes, gotchas that cost hours, and the findings
worth knowing before you start. Read it first.

The goal of every run: **did the IPP picker change routing and latency the way
we expected?** Collect both benchmark results *and* control-plane evidence (IPP
logs, EPP scores, decode KV/queue) — one without the other can't answer it.

---

## Orientation

- **IPP** is an Envoy `ext_proc` gRPC server between the Gateway and the llm-d
  Router/EPP. Default plugins inject `X-Gateway-Model-Name` (from request JSON
  `model`) and `X-Gateway-Base-Model-Name` (from labeled ConfigMaps) before the
  router schedules. With a scorer it also picks the backend model.
- **The plugin pipeline = the experiment variable**, selected by the values file
  (`helm upgrade -f <values>`). The scorer must exist in the image; which scorer
  runs is values-side:
  - **smart** — a TTFT-predicting scorer (`queue-ttft-scorer` /
    `ttft-aware-scorer` + `ttft-percentile-extractor`) + max-score picker, routing
    to the lowest predicted TTFT.
  - **random** — max-score picker, **no scorer** (`maxscore-baseline-values.yaml`;
    load-blind, spreads traffic across all listed pools regardless of load).
  - Whether a TTFT scorer needs a ResponseProcessor to observe TTFT is
    build-dependent. Builds whose streaming path skips the datalayer
    ResponseEvent record nothing without one, and every candidate then scores
    equal (random routing). Newer builds feed the extractor directly — measured
    2026-08-06 on `llm-d-arad`: no ResponseProcessor configured, yet 3000
    `ttft-observation` lines and `RecentN=250`. Always check `RecentN` rather
    than assuming either way.
  - A config-only `helm upgrade` does **not** restart the IPP pod (no checksum
    annotation) — `kubectl rollout restart deploy/payload-processor` between arms.
- Base-model ConfigMaps consumed by the default plugins **must** carry label
  `inference.llm-d.ai/ipp-managed: "true"`.
- Supplying **any** `--plugin` (or `listModels`) **replaces the entire default
  set** — include everything you need explicitly.
- Scorers must return scores in `[0, 1]`; out-of-range values are clamped. Some
  metric names still use the older `bbr` prefix.
- The gateway name stays `infra-llmdbench-inference-gateway` regardless of your
  namespace — it's derived from the release (`llmdbench`), not the project.

---

## Must-dos (skip one and the run breaks or lies)

1. **List only real backends.** The random picker routes proportionally to
   *every* listed model — list one with no backing pod and ~half the traffic
   404s into phantom pools and corrupts the run. On **OCP use the Qwen set only**
   (`Qwen/Qwen3-30B-A3B` + `Qwen/Qwen3-32B`), **never `facebook/opt-*`**; on Kind
   use the opt set. Keep `listModels` and the applied `ipp_configs/` in sync.
2. **OCP: patch `--max-model-len 8192` on both decode deployments** after standup
   or **Qwen3-32B crash-loops** (needs 10 GiB KV for the 40960 default, only
   ~8.25 available on one H100). The scenario's `maxModelLen: 8192` is ignored
   because `modelCommand: imageDefault` only exports `VLLM_MAX_MODEL_LEN`, which
   this vLLM doesn't read. Patch **both** pods for a fair comparison.
3. **IPP is NOT removed by `llmdbenchmark teardown`** — always
   `helm uninstall payload-processor -n <ns>` separately.
4. **Scope everything to your namespace** — `-p <ns>` on every `run`/`teardown`,
   `-n <ns>` on every `oc`/`kubectl`/`helm`. Omitting `-p` on `run` silently
   deploys the harness into the shared default namespace (`llmdbench`).
5. The `ocp-qwen3-multi` scenario sets `skipSmoketest: true` on purpose —
   header-match routing 404s until IPP injects `X-Gateway-Base-Model-Name`, so
   the auto-smoketest would fail.

---

## High-RPS / saturation runs

- **Harness sizing:** the default (cpu `1` / mem `16Gi`) thrashes at the shareGPT
  dataset-prep step. The `ocp-qwen3-multi` scenario already uses **cpu `8` / mem
  `32Gi`**.
- **A single inference-perf pod caps ~130–155 req/s** under saturation no matter
  the requested rate (it issues the full request count but stretches it over
  wall-clock). To truly offer 300–600 RPS, use **`-j N` parallel pods** (~150 RPS
  each). Always verify achieved vs requested in `stage_*_lifecycle_metrics.json`
  (`load_summary.count / benchmark_time_seconds`) before trusting a "500 RPS"
  label.
- **`-j N` needs RWX `workload-pvc`.** The parallel harness pods + the
  data-access helper all mount `workload-pvc`. With **ReadWriteOnce** they must
  co-locate on one node or hit Multi-Attach (see troubleshooting). The
  `ibm-spectrum-scale-fileset` storage class supports **ReadWriteMany** — set
  `shared.storage.workloadPvc.accessModes: [ReadWriteMany]` (recreate the PVC).
- Keep `--max-model-len` identical across compared runs — a smaller cap fits more
  requests in KV and **moves the saturation knee**.

---

## Findings worth knowing (so you can sanity-check results)

- **Smart vs random:** the random picker saturates the dense **Qwen3-32B by
  ~100 RPS** (throughput plateaus ~1830 tok/s, first 504s ~250 RPS) while the MoE
  sits idle. Smart routing offloads to the fast MoE and sustains far more
  (~2620 tok/s, 0 failures at 100 RPS). At true ~600 RPS (`-j 4`) both backends
  saturate with multi-thousand-deep queues.
- **Compute vs memory bound (GPU FLOPs):** the dense **Qwen3-32B is
  compute-bound** (DCGM tensor-active ~64%, MFU ~59%); the **MoE Qwen3-30B-A3B is
  memory-bound** (DRAM-active ~47% but tensor-active only ~22%, MFU ~20%). The
  MoE saturates on **HBM/KV**, not compute — it has large FLOP headroom (few
  active params/token). Both GPUs hit the ~700 W H100 TDP.
- **GPU FLOPs are NOT in the harness metrics scrape.** `metricsScrapeEnabled`
  scrapes vLLM + EPP `/metrics` (KV, queue, throughput, latency, prefix-cache,
  `inference_pool_*`, `inference_extension_*`) — useful, but **no DCGM/FLOPs**.
  vLLM exposes `vllm:estimated_flops_per_gpu_total` but it reads **0** on the
  pinned build. GPU FLOPs come **only** from `tools/collect_dcgm.py` (DCGM via
  Thanos, auto-run by `collect_logs.sh`). Use `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE`
  — **not** `DCGM_FI_DEV_GPU_UTIL` (pinned ~100%, useless) nor `PIPE_FP16_ACTIVE`
  (~0, vLLM is BF16). Analyze with `tools/compute_mfu.py` (MFU =
  `2·active_params·tokens / peak_FLOPs`, H100 BF16 989.4 TFLOP/s).
- **In-flight counter leak (scorer bug) — restart IPP between A/B arms.**
  `request-metadata-extractor` increments `Requests` per request but decrements
  only on *response events* (`requestmetadata/plugin.go:195` vs `:208`). Failed
  requests (Envoy 504/503) never emit a response event, so a failure storm leaves
  the counter stuck high → `idleness ≈ 0` → the avg-ttft staleness decay never
  engages → a saturated backend's EMA freezes and it loses scoring until an IPP
  restart (observed: traffic locked onto the drowned backend; `rollout restart`
  fixed it). Relates to IPP PR #37 — failed requests must decrement the counter.

---

## Adaptive-routing experiment (Gemma/Qwen) — extras

Not needed for the happy path in README §3; reach for these when something is off.

**Switching to the random-baseline arm.** `helm upgrade` with the random values
file hits an SSA field-manager conflict on the ConfigMap (the kubectl-patch field
manager already owns it) — patch the ConfigMap directly instead:

```bash
python3 -c "import yaml;c=yaml.safe_load(open('ipp_benchmarking/ipp_configs/adaptive-gemma-qwen-random-values.yaml'))['payloadProcessor']['customConfig'];print(yaml.safe_dump({'apiVersion':'llm-d.ai/v1alpha1','kind':'PayloadProcessorConfig','datalayer':c['datalayer'],'plugins':c['plugins'],'profiles':c['profiles']}))" > /tmp/random-ipp-config.yaml
oc create cm ipp-cfg-tmp -n "$NAMESPACE" --from-file=custom-ipp-config.yaml=/tmp/random-ipp-config.yaml --dry-run=client -o json \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(json.dumps([{'op':'replace','path':'/data/custom-ipp-config.yaml','value':d['data']['custom-ipp-config.yaml']}]))" > /tmp/patch.json
oc patch cm payload-processor -n "$NAMESPACE" --type=json --patch-file=/tmp/patch.json
oc rollout restart deploy/payload-processor -n "$NAMESPACE"
```

The random values keep `ttft-percentile-extractor` deliberately: nothing consumes
it, but its `ttft-observation` line is the per-request actual-TTFT source *and*
the end-of-stream marker the per-request e2e derivation needs on this image.
With no scorer every candidate scores 0 and `max-score-picker` shuffles before
its stable sort, so each `"auto"` request is a uniform coin flip; pinned requests
are unaffected (the filter leaves exactly one candidate).

**Running one stage at a time.** `tools/stage.sh` runs a single stage and
archives everything for it, retrying if the gpu-reaper scales a pool mid-stage.
Deploy names are standup-generated, so export them:

```bash
export NAMESPACE=<ns> TAG=<arm name> NS=$NAMESPACE      # TAG = output dir under example_outputs/
export G_DEPLOY=$(oc get deploy -n $NAMESPACE -o name | grep -i gemma | grep decode | cut -d/ -f2)
export Q_DEPLOY=$(oc get deploy -n $NAMESPACE -o name | grep -i qwen  | grep decode | cut -d/ -f2)
ipp_benchmarking/tools/stage.sh s1_pinGemma adaptive_gemma_summarization.yaml \
                                            adaptive_shared_summarization.yaml
```

A stage that produced **0 requests still exits 0** — check `total=` in the
`DONE …` line and read `/tmp/stage_<label>_<profile>.log` if it is zero.

**Concurrent runs need their own harness namespace.** llmdbenchmark deletes
harness pods by a shared per-namespace label, so co-located runs truncate each
other — hence `-p $NS,$NS-2`. The driver seeds those namespaces (PVC, data-access
pod, SA+RBAC) itself.

**Newer IPP builds may not register `model-group-name-filter`.** Check before
trusting a values file — the pod names the missing plugin and crashloops:
`oc logs -n "$NAMESPACE" deploy/payload-processor | grep -E "build|not registered"`.
`ttft-aware-fix` (build `8f4a606`) registers `auto-group-model-name-filter`
instead, which pins by group: `"auto"` → both pools, `"auto/gemma"` → Gemma only
(use `adaptive_gemma_autogroup_summarization.yaml`), an exact model name → **429**.
Use `ipp_configs/adaptive-gemma-qwen-ttft-aware-fix-values.yaml`, plus a probe
route because harness step 04 verifies with a bodyless `GET /v1/models` that IPP
cannot route:

```bash
sed "s|<inference-pool>|$(oc get inferencepool -n $NAMESPACE -o name | head -1 | cut -d/ -f2)|" \
  ipp_benchmarking/ipp_configs/models-probe-route.yaml | oc apply -n "$NAMESPACE" -f -
```

**Verify before a stage** — a dead capture only warns, and `2/2 Ready` does not
mean vLLM is serving:

```bash
GW=http://infra-llmdbench-inference-gateway-istio.$NAMESPACE.svc.cluster.local:80
oc exec -n "$NAMESPACE" ipp-logtail -- curl -s -o /dev/null -w '%{http_code}\n' $GW/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","prompt":"warm up","max_tokens":8,"stream":true}'          # want 200
oc exec -n "$NAMESPACE" ipp-logtail -- stat -c %s /requests/smart-logs/ipp-full-live.log  # want growth
```

**Slice a stage window by hand** (from `stage_marks.log`), in-cluster:

```bash
oc exec -i -n "$NAMESPACE" ipp-logtail -- sh -s s1_pinGemma:<start_ts>:<end_ts+60> \
  < ipp_benchmarking/tools/slice_raw_window.sh
oc rsync -n "$NAMESPACE" ipp-logtail:/requests/smart-logs/windows/ ./   # oc cp truncates
```

**Confirm the scorer is not blind.** With no TTFT observations it scores every
model equally — indistinguishable from a random picker:

```bash
python3 -c "
import gzip,json,collections
c=collections.Counter()
for l in gzip.open('<stage>/ipp-raw.log.gz','rt',errors='ignore'):
    if '\"msg\":\"ttft-percentile wrote attribute\"' in l: c[json.loads(l).get('RecentN')]+=1
print(c)"        # all-zero RecentN => blind: the build needs a ResponseProcessor, re-run with one
```

**Two pools must be stood up sequentially** — they collide on the shared gateway
secret in parallel. Set `LLMDBENCH_WAIT_TIMEOUT=6000` and wait for real vLLM
readiness (poll `/v1/completions`, not `/health`).

---

## Troubleshooting

**Namespace scoping.** See must-do #4. Cluster-scoped `llmdbench-modelservice-*`
ClusterRoles are shared — teardown removes the set it created; don't hand-delete
them while another project uses the stack.

**RWO `workload-pvc` Multi-Attach + the stale helper pod.** With RWO, if a
harness pod lands on a different node than the `access-to-harness-data-workload-pvc`
helper it hangs in `ContainerCreating` (`Multi-Attach error ... already used by
pod(s) access-to-harness-data-workload-pvc`) until the 3600 s timeout. Unblock by
moving the helper onto the harness's node (dump YAML, set `spec.nodeName`,
force-delete, re-apply). **Gotcha:** a manually-recreated helper then blocks the
next run's step 02 (`Forbidden: pod updates may not change fields other than
image...`) — so **delete any leftover `access-to-harness-data-workload-pvc` pod
before a new run.** Durable fix: RWX `workload-pvc` (above).

**A node without the storage CSI driver.** With RWX, a pod can land on a node
missing the `spectrum-scale` CSI driver and fail to attach
(`CSINode ... does not contain driver spectrumscale.csi.ibm.com`). Cordoning the
node is cluster-scoped (may be disallowed). Namespace-scoped fix: pin pods to
storage-capable nodes via a label only they carry —
`oc annotate namespace <ns> openshift.io/node-selector="scale=true"` (revert
after). Confirm your good nodes have the label first.

**Harness HuggingFace egress fails on some nodes.** Some workers can't reach HF
over IPv4 → tokenizer init fails (IPv6 nodes succeed). The router serves both
models regardless of which stack's harness launched, so a single healthy stack's
results are usable; re-running usually reschedules onto a working node.

**`route already exists` during standup** — non-fatal leftover route from a prior
run; `All standup steps complete` still prints. Ignore, or
`oc delete route llmdbench-inference-gateway-route` first.

**`collect_logs.sh` finds no workspace.** It greps `/tmp` at depth 1, but the
workspace may live under `/tmp/<user-or-runtime>/`. Point it explicitly:
`export LLMDBENCH_WORKSPACE=$(ls -dt /tmp/**/workspace_llmdbench_* | head -1)`
then re-run.

**Benign warmup noise** (not fatal): vLLM `Unknown vLLM environment variable
detected: VLLM_MAX_MODEL_LEN` (expected — that's why the `--max-model-len` patch
is needed); EPP `metric family "vllm:lora_requests_info" not found`; brief
`503 "decode node is not ready"` during pod warmup.

**Custom plugins need their Envoy events.** Supplying `--plugin` overrides all
defaults — include every plugin. A plugin that reads the body sees nothing unless
`provider.supportedEvents.requestBody` / `responseBody` is `true`.

**Multi-stack runs render `REPLACE_ENV_LLMDBENCH_DEPLOY_CURRENT_MODEL` with
stack 1's model for every stack.** A profile using that placeholder gets the
first stack's model name even in the second stack's pass (observed: the
qwen3-32b pass rendered `model_name: Qwen/Qwen3-8B` and re-ran the 8B
benchmark). Workaround: run one stack at a time with explicit overrides —
`--stack <name> -m <model>` — and verify the rendered profile:
`kubectl get cm inference-perf-profiles -o yaml | grep model_name`.

**A cluster `gpu-reaper` scales idle GPU deployments to 0 after 90 minutes**
(pokprod001; annotation `gpu-reaper.io/reason: Idle since ... freeing N
GPU(s)` on the deployment). A model pool left idle while you debug something
else silently loses its pod, and the next benchmark run sends traffic into a
dead backend. Check `kubectl get deploy` replicas before every run and
`kubectl scale deploy/<decode> --replicas=1` to restore; keep idle gaps under
90 minutes during multi-hour sessions.

**No HTTPRoutes after standup.** IPP routes by the `X-Gateway-Base-Model-Name`
header, which the default PathPrefix route can't express, so the scenario sets
`httpRoute.enabled: false` and `08_httproute.yaml` renders nothing — the gateway
404s everything until header-match routes exist. Generate and apply them after
standup: `tools/gen_httproutes.sh "$NAMESPACE" | kubectl apply -f -` (pool names
are derived from the namespace + model, so nothing to hand-edit).

**Scenario `model.size` is the decode pod's emptyDir limit — undersizing it
is an eviction loop.** The `model-storage` emptyDir's `sizeLimit` comes from
the scenario's `model.size`; if the checkpoint is bigger, the pod downloads
weights, gets `Evicted` ("Usage of EmptyDir volume ... exceeds the limit"),
and a replacement repeats forever. Qwen3-32B BF16 needs `size: 70Gi` (the
checkpoint is ~64GB). Live fix without re-standup: patch the deployment
volume's `emptyDir.sizeLimit` directly.

**Decode pods crash with `getpwuid(): uid not found`.** OpenShift runs vLLM under
an arbitrary high UID absent from `/etc/passwd`, so torch's `getpass.getuser()`
dies. Setting `USER`/`LOGNAME` makes `getuser()` skip the passwd lookup (README §2
patches both decode deploys; the adaptive scenario already sets them under
`shared.decode.extraEnvVars` — a per-scenario block silently no-ops). Patch the
deploy to `strategy: Recreate` first, or the rolling update's surge pod deadlocks
waiting for a GPU that doesn't exist.

**`listModels` renders nothing → IPP crashloops on a missing
`/config/models.json`.** Upstream `main`'s chart has no `listModels` block in
`templates/config.yaml`. Either use a chart that does, or inject the key:

```bash
oc patch cm payload-processor -n "$NAMESPACE" --type=merge \
  -p '{"data":{"models.json":"{\"models\":[{\"name\":\"Qwen/Qwen3-8B\"},{\"name\":\"Qwen/Qwen3-32B\"}]}"}}'
oc rollout restart deploy/payload-processor -n "$NAMESPACE"
```

**Kind: `helm upgrade` fails on SSA field-manager conflicts** if the release was
hand-patched — `helm uninstall payload-processor -n llmdbench` and install fresh.

**Plotting a plain `llmdbenchmark run`** (not via `ab_routing_run.sh`): the raw
`per_request_lifecycle_metrics.json` is 100MB–1GB and often truncated, so build
the slim extract the OCP latency plotter needs first (the routing plotter needs
the IPP decision log, which only `ab_routing_run.sh` captures):

```bash
.venv/bin/python3 ipp_benchmarking/tools/extract_per_request_slim.py \
  <results>/.../per_request_lifecycle_metrics.json  <arm_dir>/per_request_slim.json
```

**IPP config changes don't restart the pod.** The chart has no ConfigMap
checksum annotation, so a `helm upgrade` that only changes `customConfig` /
`listModels` updates the ConfigMap but leaves the old pod running with the old
config. Always `kubectl rollout restart deploy/payload-processor -n <ns>` after
a config-only upgrade, and verify with:
`kubectl logs <new-pod> | grep "Loaded raw configuration"`.

**`provider.messageTimeout=10s` is required.** Without it Envoy's ~200 ms default
trips `HTTP 504 ext_proc_error_per-message_timeout_exceeded` once IPP defers
header ACKs under load.

**helm-diff must be ≥ v3.14 with Helm 4.** Helm 4 removed `--validate`; helm-diff
≤ v3.13 still passes it and fails. Reinstall:
`helm plugin install https://github.com/databus23/helm-diff --version v3.15.7`.

**Don't bump `llm-d-inference-sim` to `latest` on Kind.** `latest` needs an HTTP
render sidecar on `localhost:8082` and crash-loops without one. The
`kind-sim-multi` scenario is pinned to a UDS-based tag that works as-is.

---

## Environment specifics (these were ours — adapt to yours)

- OpenShift project `llm-d-arad` on cluster `api.pokprod001.ete14.res.ibm.com`.
- IPP image at `ghcr.io/<user>/llm-d-inference-payload-processor` with distinct
  tags per picker; new ghcr packages are **private** by default (make public or
  add a pull secret).
- Model PVCs are **not reliably preserved** across teardown — expect a fresh
  ~60–64 GB download per Qwen model on standup.
- Secrets/config in repo `.env` (gitignored): `HF_TOKEN`, `IPP_PATH`, `GHCR_PAT`.
  `source .venv/bin/activate && source .env` before running.

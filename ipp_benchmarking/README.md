# Running multi-model IPP benchmarks

Evaluate IPP routing (a **smart** load-aware picker vs. a **random** baseline)
end-to-end with [llm-d-benchmark](https://github.com/llm-d/llm-d-benchmark).

**Read [`AGENTS.md`](./AGENTS.md) first** — it has the required fixes, gotchas,
and findings (the "must-dos" and troubleshooting are there, not here).

`llmdbenchmark` discovers this folder automatically:
`--spec cicd/ocp-qwen3-multi` and `-w saturation_sweep_500.yaml` resolve the
bundle's scenarios/specs/profiles, while shared templates still come from the
repo root. Just **run `llmdbenchmark` from the repo root** (its default
`base_dir` is the cwd). 

Contents: `tools/` (GPU/MFU + plotting), `ipp_configs/` (base-model ConfigMaps),
`config/` (the `kind-sim-multi` + `ocp-qwen3-multi` scenarios & specs),
`workload/profiles/inference-perf/` (saturation/scrape profiles),
`collect_logs.sh`, and `example_outputs/` (sample comparison plots + story
HTMLs). Raw `collected-logs-` run data isn't included (too large).

Two scenarios:
- **Kind** (`cicd/kind-sim-multi`) — `llm-d-inference-sim` pods, no GPU. Two
  fake-latency models: `facebook/opt-125m` (slow) + `opt-350m` (fast).
- **OpenShift** (`cicd/ocp-qwen3-multi`) — real vLLM on H100-80GB.
  `Qwen3-30B-A3B` (MoE, fast) + `Qwen3-32B` (dense, slow).

The IPP wiring (helm install, base-model ConfigMaps) is shared; only the cluster
/ scenario / image-loading differs. IPP images: build a **smart** and a
**random** variant and swap with `helm upgrade` (see AGENTS.md).

---

# A. Kind (simulator)

```bash
# 1. cluster + harness image
kind create cluster
docker pull ghcr.io/llm-d/llm-d-benchmark:v0.6.3
kind load docker-image ghcr.io/llm-d/llm-d-benchmark:v0.6.3

# 2. build + side-load the IPP image (from the IPP repo; tag per scorer-picker)
make image-build
kind load docker-image ghcr.io/llm-d/llm-d-inference-payload-processor:smartRouting

# 3. install llmdbenchmark, then stand up the stack
./install.sh && source .venv/bin/activate
llmdbenchmark --spec cicd/kind-sim-multi standup -p llmdbench
```

Deploy IPP:

```bash
export IPP_PATH=/path/to/llm-d-inference-payload-processor
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n llmdbench --set provider.name=istio \
  --set payloadProcessor.image.tag=smartRouting \
  --set payloadProcessor.image.pullPolicy=Never \
  --set provider.supportedEvents.requestHeaders=true \
  --set provider.supportedEvents.requestBody=true \
  --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseHeaders=false \
  --set provider.supportedEvents.responseBody=true \
  --set provider.supportedEvents.responseTrailers=false \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway \
  --set provider.messageTimeout=10s \
  --set 'payloadProcessor.listModels[0]=facebook/opt-125m' \
  --set 'payloadProcessor.listModels[1]=facebook/opt-350m'
kubectl apply -n llmdbench -f ipp_benchmarking/ipp_configs/opt-125m-base-model.yaml \
                           -f ipp_benchmarking/ipp_configs/opt-350m-base-model.yaml
```

Give the sim models different latency so routing has something to optimize
(slow `opt-125m`: TTFT 3s / ITL 200ms; fast `opt-350m`: TTFT 1s / ITL 50ms):

```bash
for dep in facebook-16adac56-opt-125m-decode facebook-16adac56-opt-125m-prefill; do
  kubectl patch deployment "$dep" -n llmdbench --type=json -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--time-to-first-token=3s"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--inter-token-latency=200ms"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-num-seqs=10"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-waiting-queue-length=20"}]'
done
for dep in facebook-a5673f1c-opt-350m-decode facebook-a5673f1c-opt-350m-prefill; do
  kubectl patch deployment "$dep" -n llmdbench --type=json -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--time-to-first-token=1s"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--inter-token-latency=50ms"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-num-seqs=10"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-waiting-queue-length=20"}]'
done
```

Run, collect, tear down:

```bash
llmdbenchmark --spec cicd/kind-sim-multi run -l inference-perf -w sanity_random.yaml
NAMESPACE=llmdbench ./ipp_benchmarking/collect_logs.sh     # -> ./collected-logs-<N>/
llmdbenchmark --spec cicd/kind-sim-multi teardown -p llmdbench
helm uninstall payload-processor -n llmdbench               # IPP isn't torn down automatically
```

---

## Mac + Colima quick-start (CostGuard)

Same `cicd/kind-sim-multi` scenario as `# A` above, but on macOS via
[Colima](https://github.com/abiosoft/colima). Docker-for-Mac substitutes work
too, but Colima is what this path is tested on. The cluster side needs three
extras that plain-Docker Linux gets for free — MetalLB to give the Istio
Gateway a real IP, a static route from the Mac into the Colima VM so that IP
is reachable, and an iptables `FORWARD` rule inside the VM. The bootstrap
script below sets all of that up, then runs `llmdbenchmark ... standup` and
patches the two sims so they have **asymmetric latency and completion-token
output** (opt-125m: TTFT 3s, ITL 200ms, max-tokens 512; opt-350m: TTFT 1s,
ITL 50ms, max-tokens 64). That asymmetry is the routing decision the
[CostGuard](https://github.com/kubernetes-sigs/gateway-api-inference-extension/)
picker is being evaluated on: one backend is slower **and** produces more
completion tokens per response than the other.

**Prereqs (Homebrew):** `colima`, `docker` CLI, `kind`, `kubectl`, `helm`,
`bash 4+`, `sudo` for the route add. Pinned versions the script drives:
kind node `v1.34.0`, MetalLB `v0.15.2`, benchmark image `v0.7.0`, sim `v0.8.2`.

```bash
./ipp_benchmarking/tools/mac_colima_bootstrap.sh
```

The script is idempotent — re-running skips Colima/kind/MetalLB steps that
are already done. Pass `--skip-standup` to only refresh the cluster/MetalLB
side without re-running `llmdbenchmark standup`. On completion it prints the
Gateway's MetalLB IP, a smoke-test `curl` line, and the exact `helm upgrade`
block to run next.

What the script leaves ready: Colima up, single-node kind cluster,
MetalLB with a pool in the kind Docker-network CIDR (last-octet .200–.250),
the `cicd/kind-sim-multi` stack stood up in namespace `llmdbench` with two
`InferencePool`s + two header-match `HTTPRoute`s (created via
`gen_httproutes.sh` as a fallback for the modelservice chart), and both
decode sims patched with the asymmetric flags above. **IPP is not
installed** — that's the CostGuard step below.

Install IPP with your CostGuard values file:

```bash
export IPP_PATH=/path/to/llm-d-inference-payload-processor
./ipp_benchmarking/tools/ipp_deploy.sh
```

`ipp_deploy.sh` builds the IPP image from `$IPP_PATH` (`make image-kind`),
side-loads it into the kind node, and helm-installs the chart with the
default CostGuard values file at
[`ipp_configs/kind-costguard/costguard-kind-values.yaml`](./ipp_configs/kind-costguard/costguard-kind-values.yaml).
That file wires the `costguard` scorer + `model-cost-extractor` extractor
+ `model-config-datasource` populated from a `payloadProcessor.models`
block (rendered as `/config/models.json` inside the pod). See the file
for tunable pricing values.

Run + collect + teardown:

```bash
llmdbenchmark --spec cicd/kind-sim-multi run -l inference-perf -w sanity_random.yaml
NAMESPACE=llmdbench ./ipp_benchmarking/collect_logs.sh
helm uninstall payload-processor -n llmdbench
llmdbenchmark --spec cicd/kind-sim-multi teardown -p llmdbench
```

Or use the `Makefile` targets: `make sim-colima IPP_PATH=...` runs
bootstrap + deploy in one shot; `make tear-down-sim` wipes everything.

**Exercising the CostGuard cost signal (large `max_tokens`).** The upstream
`sanity_random.yaml` profile is a generic smoke test — it samples
`max_tokens ~ N(mean=50, max=100)`, which is small enough that
`llm-d-inference-sim` frequently falls back to its default
`Gaussian(mean=40, sd=20)` output length. Both sims then produce ~identical
responses, the deliberate `--max-model-len` asymmetry described above
(opt-125m len=512 vs opt-350m len=64) never materializes on the wire, and
CostGuard has no cost delta to score against. To force the asymmetry into
per-request metrics, use the dedicated profile shipped alongside CostGuard:

```bash
llmdbenchmark --spec cicd/kind-sim-multi run \
  -l inference-perf -w kind-costguard-large-tokens.yaml
NAMESPACE=llmdbench ./ipp_benchmarking/collect_logs.sh
```

The profile
([`workload/profiles/inference-perf/kind-costguard-large-tokens.yaml.in`](./workload/profiles/inference-perf/kind-costguard-large-tokens.yaml.in))
pins client-side `max_tokens=512` (via `data.output_distribution` with
`std_dev=0`) so opt-125m runs to its 512-token cap while opt-350m's server-side
`--max-model-len=64` truncates output to ~48 tokens — two clean output-length
modes, deterministically. Input is pinned tiny (`mean=16`) to keep
`input_len + max_tokens` clear of opt-350m's model-length ceiling; if
`input_len` exceeded the cap the 350m stack would reject requests entirely
rather than truncate, which would *mask* the asymmetry rather than expose it.
`model_name` is a JSON-array-encoded string (`'["facebook/opt-125m","facebook/opt-350m"]'`),
so IPP's `model-group-name-filter` treats both as candidates and hands the routing
decision to the CostGuard scorer — a single-model `model_name` would pin
routing and defeat the test. `api.type: completion` means the wire field is
OpenAI's `max_tokens` (not `max_completion_tokens`, which is the
`chat/completions` field), and `ignore_eos: true` prevents small models from
stopping early regardless of the client cap. Successful runs should show two
distinct output-length clusters in
`per_request_lifecycle_metrics.json` (~512 tokens for opt-125m routes, ~48 for
opt-350m routes) and non-trivial per-request cost deltas in the
`payload-processor` pod logs.

Note: `-o "max_tokens=512"` on the `llmdbenchmark run` command line **does not
work** for inference-perf profiles — the override walker only writes to keys
whose parent dict already exists, and inference-perf has no top-level
`max_tokens`. Authoring a dedicated profile (as above) is the correct path;
the equivalent dotted overrides would be
`-o "data.output_distribution.mean=512,data.output_distribution.min=512,data.output_distribution.max=512,data.output_distribution.std_dev=0"`.

**Mac-specific gotchas.**

+ The `sudo route add` in the script will prompt for your password once.
+ If Colima was already running the script does **not** restart it — it
  reuses the running VM. If you want a clean-slate Colima VM, run
  `colima stop && colima delete default` first.
+ The MetalLB pool is derived from `docker network inspect kind`. The kind
  network CIDR is stable across cluster re-creates on the same host, but if
  you `docker network prune` between runs the pool will shift; re-run the
  script to reconfigure.
+ IPP config changes don't restart the pod (chart has no ConfigMap checksum
  annotation) — after a `helm upgrade` that only changes `customConfig` or
  `listModels`, run
  `kubectl rollout restart deploy/payload-processor -n llmdbench` and
  verify the new pod picked it up: `kubectl logs <new-pod> | grep "Loaded raw configuration"`.

---

# B. OpenShift (real GPUs)

Prereqs: `oc whoami` works, ≥2 nodes with `NVIDIA-H100-80GB-HBM3`, NVIDIA GPU
Operator, a ≥200Gi StorageClass. Set your project once: `export NAMESPACE=llm-d-<user_name>`.

```bash
# namespace + HF secret
oc new-project "${NAMESPACE}" || oc project "${NAMESPACE}"
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxx
```

build IPP to a registry the cluster can pull from (if its a private ghcr -> [make public in your account](https://docs.github.com/en/packages/learn-github-packages/configuring-a-packages-access-control-and-visibility#configuring-visibility-of-packages-for-your-personal-account))
```bash
echo $GHCR_PAT | docker login ghcr.io -u <user_name> --password-stdin
VERSION=smartRouting make image-build
docker push ghcr.io/<user_name>/llm-d-inference-payload-processor:smartRouting

# stand up (pulls ~60GB + ~64GB Qwen weights to the model PVC)
llmdbenchmark --spec cicd/ocp-qwen3-multi standup -p "${NAMESPACE}"
```

Deploy IPP:

```bash
export IPP_PATH=/path/to/llm-d-inference-payload-processor
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n "${NAMESPACE}" --set provider.name=istio \
  --set payloadProcessor.image.registry=ghcr.io/<user_name> \
  --set payloadProcessor.image.repository=llm-d-inference-payload-processor \
  --set payloadProcessor.image.tag=smartRouting \
  --set payloadProcessor.image.pullPolicy=IfNotPresent \
  --set provider.supportedEvents.requestHeaders=true \
  --set provider.supportedEvents.requestBody=true \
  --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseHeaders=false \
  --set provider.supportedEvents.responseBody=true \
  --set provider.supportedEvents.responseTrailers=false \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway \
  --set provider.messageTimeout=10s \
  --set 'payloadProcessor.listModels[0]=Qwen/Qwen3-30B-A3B' \
  --set 'payloadProcessor.listModels[1]=Qwen/Qwen3-32B'
kubectl apply -n "${NAMESPACE}" -f ipp_benchmarking/ipp_configs/qwen3-30b-a3b-base-model.yaml \
                                -f ipp_benchmarking/ipp_configs/qwen3-32b-base-model.yaml
```

**Required after standup** — patch `--max-model-len 8192` on both decode pods or
Qwen3-32B crash-loops (see AGENTS.md for why):

```bash
for dep in qwen-qwe-de682216-wen3-32b-decode qwen-qwe-65acc96e--30b-a3b-decode; do
  kubectl patch deployment "$dep" -n "${NAMESPACE}" --type=json -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-model-len"},
    {"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"8192"}]'
done
# deployment names carry a per-standup hash — confirm with: kubectl get deploy -n "${NAMESPACE}"
```

Run, collect, tear down:

```bash
llmdbenchmark --spec cicd/ocp-qwen3-multi run -l inference-perf -w saturation_sharegpt.yaml -p "${NAMESPACE}"
NAMESPACE="${NAMESPACE}" ./ipp_benchmarking/collect_logs.sh   # IPP/EPP/decode logs + benchmark-results + gpu-dcgm/
llmdbenchmark --spec cicd/ocp-qwen3-multi teardown -p "${NAMESPACE}"   # add -d to also clear the model PVC
helm uninstall payload-processor -n "${NAMESPACE}"
```

## Saturation sweeps

Profiles in `workload/profiles/inference-perf/`: `saturation_sweep.yaml`
(200/250/300) and `saturation_sweep_500.yaml` (200/300/400/500), each stage 25 s
load + 25 s cooldown. A/B the pickers by `helm upgrade`-ing `image.tag` between
runs (no teardown). **A single harness caps ~150 RPS — use `-j N` for higher**
(see AGENTS.md for the RWX/node requirements).

```bash
llmdbenchmark --spec cicd/ocp-qwen3-multi run -l inference-perf \
  -w saturation_sweep_500.yaml -p "${NAMESPACE}" -j 4
```

Compare with the plot tools (in `ipp_benchmarking/tools/`), passing
`random=collected-logs-N smart=collected-logs-M`: `plot_saturation_comparison.py`,
`plot_latency_and_failures.py`, `plot_picker_decisions_v2.py`.

## GPU compute / FLOPs (MFU)

`ipp_benchmarking/collect_logs.sh` auto-archives per-GPU DCGM metrics to
`collected-logs-<N>/gpu-dcgm/`. Plot with `ipp_benchmarking/tools/query_gpu_utilization.py`;
compute MFU with `ipp_benchmarking/tools/compute_mfu.py`. (Dense = compute-bound,
MoE = memory-bound — see AGENTS.md.)

## OpenShift A/B: smart routing vs static single-model baselines (ocp-qwen3-8b-32b)

Alternate OCP scenario `cicd/ocp-qwen3-8b-32b`, orchestrated by
`tools/ab_routing_run.sh`.

**One script, one run at a time.** `tools/ab_routing_run.sh <arm> <ipp_values_file>
[profile]` runs exactly one thing per invocation — it patches the values file's
routing config into the IPP, restarts, runs the given profile (no planner, so the
decision log is clean), streams the **full IPP log** live to `ipp-full-live.log`
(so per-request predicted/actual TTFT survives the kubelet's container-log
rotation, which at `v=4` under load drops early stages within minutes), then
collects logs + the slim extract. The A/B is three such runs:

- **static baselines** — register **only one model** (the values file's `listModels`
  has a single entry, so there is no routing choice) and drive its **half-conc**
  profile. `static-8b-only-values.yaml` + `half_8b.yaml` and
  `static-32b-only-values.yaml` + `half_32b.yaml`, each C=25→275→25 so that
  **8B@C/2 + 32B@C/2 sums to the smart run's full C**. The static split has no spill
  path: when the 8B leg saturates there is nowhere to offload.
- **smart** (`median-ttft-ocp-values.yaml`, **both** models registered, **no**
  `model-group-name-filter` so `sweep_8stage`'s single-string `model_name` lets the
  model-selector pick from both `listModels`): `queue-ttft-scorer` (predicts
  per-request TTFT under load via the `ttft-percentile-extractor`, routes to the
  lowest predicted TTFT, with `explorationRate: 0.1`) + `max-score-picker` keeps
  most traffic on the fast 8B and offloads overflow to the 32B as load climbs.
  Drives the full `sweep_8stage.yaml` (C=50→550→50, 11 stages, ~43k reqs,
  ~5 min/stage; the symmetric up/down legs expose routing hysteresis). Needs an
  IPP image built with the queue-ttft-scorer (set its tag in step 2). For the
  avg-ttft scorer instead, swap in `avgttft-ocp-values.yaml` (also no filter).

All timeouts are lifted (route + client `request_timeout` to 1200s, ext-proc
`messageTimeout` to 1200s) so nothing is shed — the delta is latency/throughput,
not failures. Smart keeps **85–96%** of load on the fast 8B and spills overflow
to the 32B; the static split cannot.

---

# C. Model-routing benchmark (model-name-filter + latency scorer)

Benchmarks for the "idle big model absorbs the small summarizer's overflow"
user story: a deep-research agent saturates a small summarizer model
(hundreds of summarization calls) while a large planner/synthesis model sits
idle; a latency scorer should route summarization overflow onto the idle big
model. Targets the IPP **ModelSelector** pipeline (`model-selector` +
`model-name-filter` + scorer + picker), not the smart-vs-random picker images
of sections A/B.

## The A/B mechanism — request-side, one line per variant

`model-name-filter` (IPP PR #33) pins a request to the model named in its
body, and interprets an **array** of names as "choose from this list".
inference-perf's `model_name` config is typed `str` (a YAML list fails its
pydantic validation), so multi-candidate requests use a **string-encoded JSON
array**, which the filter parses (IPP follow-up PR to #33):

```yaml
server:
  model_name: '["facebook/opt-125m","facebook/opt-350m"]'   # choose among these (scorer routes)
  # model_name: "facebook/opt-125m"                         # pinned to one model
```

Both variants run identical infra and IPP config — the A/B delta is only the
`model_name` value in the workload profile. Pinned traffic (e.g. a planner
workload naming the big model) is never re-routed; unregistered or malformed
names are rejected with HTTP 400.

## Artifacts

| File | Purpose |
|---|---|
| `ipp_configs/avgttft-blog-values.yaml` | IPP helm values: model-selector + model-name-filter + avg-ttft-scorer (1.0) + max-score-picker + request-metadata-extractor + model-config-datasource |
| `workload/profiles/inference-perf/model_array_string.yaml.in` | 20s regression check: string-array model_name end-to-end (0/100 → 100/100 across the filter fix) |
| `workload/profiles/inference-perf/blog_rehearsal_sweep.yaml.in` | 6×60s stages at 1.5/3.2/5 RPS (too gentle for the sims — kept for reference) |
| `workload/profiles/inference-perf/blog_rehearsal_sweep_hot.yaml.in` | 6×60s stages at 3/12/40 RPS — saturates the sims, exposes the routing dynamics |
| `tools/routing_split_by_stage.py` | Buckets IPP "Model selected" log lines into stages: per-stage per-model routing counts |

## Running on Kind

Stand up `kind-sim-multi` + IPP exactly as in section A (same image-build,
EnvoyFilter flags, sim latency patches), then swap the IPP config:

```bash
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n llmdbench --set provider.name=istio \
  -f ipp_benchmarking/ipp_configs/avgttft-blog-values.yaml \
  --set payloadProcessor.image.tag=<your-tag> \
  --set payloadProcessor.image.pullPolicy=Never \
  --set provider.supportedEvents.requestHeaders=true \
  --set provider.supportedEvents.requestBody=true \
  --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseHeaders=false \
  --set provider.supportedEvents.responseBody=true \
  --set provider.supportedEvents.responseTrailers=false \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway \
  --set provider.messageTimeout=10s
# ConfigMap changes do NOT restart the pod (no checksum annotation) — always:
kubectl rollout restart deploy/payload-processor -n llmdbench

llmdbenchmark --spec cicd/kind-sim-multi run -l inference-perf -w blog_rehearsal_sweep_hot.yaml
```

Analyze routing per stage (during or after a run):

```bash
kubectl logs deploy/payload-processor -n llmdbench --since=30m | \
  python3 ipp_benchmarking/tools/routing_split_by_stage.py --stages 60,60,60,60,60,60
```

Per-stage success/fail/TTFT come from `stage_*_lifecycle_metrics.json` under
the printed `Local results:` path. Note `--spec kind-sim-multi run` executes
the profile **twice** (one treatment per listed model) — back-to-back
treatments share leftover queue/EMA state, so treat treatment 2 with care.

## Rehearsal findings (2026-06-11, avg-ttft + max-score-picker on Kind sims)

**Graphs + full narrative: [`example_outputs/kind-model-routing/`](./example_outputs/kind-model-routing/README.md)**
(routing timelines, per-stage outcomes, the blended-scorer comparison, the
parallel-spike timeline; regenerate with `tools/plot_kind_rehearsal.py`).

Sim capacity with the section-A latency patches: fast opt-350m ≈ 4.5–5 RPS,
slow opt-125m ≈ 0.8 RPS (P/D split means prefill does not consume decode
slots — higher than naive estimates).

- **The pipeline works end-to-end**: string-array requests flow through
  filter → scorer → picker; unsaturated stages route ~99% to the
  lower-TTFT model with occasional exploration of the idle one.
- **Cold start is visible**: models with no TTFT data score 1.0 ("idle"), so
  the first stage shows p90 TTFT = the slow model's 3s until EMAs fill in.
- **Winner-take-all stampedes**: `max-score-picker` sends 100% of traffic to
  whichever EMA is lower. When the fast sim saturated (12 RPS stage), routing
  flipped to 100%-slow, burying the 0.8 RPS backend, then flipped back —
  full-amplitude ping-pong at roughly EMA-lag period.
- **Staleness pathology**: a backend so overloaded it stops emitting first
  tokens stops updating its EMA; after the 30s staleness threshold it is
  treated as *idle* (score 1.0) and attracts **all** traffic (observed:
  805/819 selections in one minute onto the drowned sim). "No data = idle"
  is correct at cold start and exactly backwards under overload.
- **Result**: at 12 RPS ~47% failures, at 40 RPS ~98% failures (mostly
  Envoy 504 upstream-timeout + 503 shedding), and the poisoned state bled
  into the next treatment even at 2 RPS.

**Implication for the blog A/B**: with `max-score-picker`, latency-aware
routing *loses* to static pinning once the system saturates. The overflow
story needs per-request negative feedback or exploration in the
scorer/picker combination.

**`weighted-random-picker` does not help with 2 candidates** (by inspection,
no run needed): scorers normalize min-max, so with exactly two models the
scores are always {1.0, 0.0}, and the picker's A-Res sampling
(`keyᵢ = Uᵢ^(1/wᵢ)`) gives a zero-weight model key 0 — it can never win.
Two-candidate weighted-random ≡ max-score.

**The fix that works (verified): blend `inflight-requests-scorer` (1.0) with
avg-ttft (1.0)** — inflight increments at *request* time, so each pick
immediately lowers the picked model's next score (no EMA lag). Re-running the
hot sweep: stable load-proportional split at every stage (no flips, no
lock-ins), failures in the small-saturated stages cut ~40% (450 vs 753).
Cost: while unsaturated, ~30% of traffic lands on the slower model
(successes' TTFT p90 3.0s vs 1.0s) — tune the inflight weight down (e.g.,
0.3) to trade smoothness for unsaturated latency. The exploration picker
(IPP PR #30) is the alternative lever. Note the in-flight counter leak
(below) poisons *both* scorers across failure storms — restart IPP between
A/B runs until fixed.

## OpenShift: the full A/B (ocp-qwen-gemma-multi)

Scenario `cicd/ocp-qwen-gemma-multi`: Qwen/Qwen3-8B (small/fast summarizer)
+ Qwen/Qwen3-32B (big planner; **placeholder until the "Gemma 4 31B"
HF id is confirmed** — swap points are listed in the scenario header). Gemma
is a gated HF repo: the standup `HF_TOKEN` must have accepted the license.

```bash
export NAMESPACE=llm-d-<user>
llmdbenchmark --spec cicd/ocp-qwen-gemma-multi standup -p "${NAMESPACE}"

# IPP (image from a registry the cluster pulls; see section B for the build):
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n "${NAMESPACE}" --set provider.name=istio \
  -f ipp_benchmarking/ipp_configs/blog-ocp-qwen-gemma-values.yaml \
  --set payloadProcessor.image.registry=ghcr.io/<user> \
  --set payloadProcessor.image.tag=<tag> \
  --set provider.supportedEvents.requestHeaders=true \
  --set provider.supportedEvents.requestBody=true \
  --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseHeaders=false \
  --set provider.supportedEvents.responseBody=true \
  --set provider.supportedEvents.responseTrailers=false \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway \
  --set provider.messageTimeout=10s
kubectl apply -n "${NAMESPACE}" -f ipp_benchmarking/ipp_configs/qwen3-8b-base-model.yaml \
                                -f ipp_benchmarking/ipp_configs/qwen3-32b-base-model.yaml

# REQUIRED: header-match HTTPRoutes. Current llm-d-benchmark templates no
# longer render the per-pool experimentalHttpRoute routes (older versions
# did), so without this every gateway request 404s. Check the InferencePool
# hash names first (kubectl get inferencepool) and adjust the file if needed.
kubectl apply -n "${NAMESPACE}" -f ipp_benchmarking/ipp_configs/qwen-gemma-httproutes.yaml
```

Also: the scenario's `model.size` must exceed the checkpoint size or the
decode pod enters an eviction loop, and the `--max-model-len 8192` patch
(AGENTS.md must-do #2) applies to the 32B decode deployment here too.

**Order of operations:**

1. **Calibrate**: run `summarization_synthetic.yaml` pinned to each model
   separately (single-name `model_name`, ascending ramp) to find K_small and
   K_big at the 2048-in/256-out shape; set the 6 stage rates per the comments
   in the profile (placeholders: 5/14/24 RPS).
2. **Variant A** (latency routing): planner first from a second namespace
   (next subsection), then the summarizer with the string-array `model_name`.
3. **Variant B** (pinned baseline): identical, with the summarizer's
   `model_name` set to `"Qwen/Qwen3-8B"` only. **Restart IPP between
   variants** (in-flight counter leak — see findings).
4. Collect with `collect_logs.sh` (includes DCGM for the GPU-util headline
   chart); analyze routing with `tools/routing_split_by_stage.py`
   (`--stages 120,120,120,120,120,120`) and plot per-stage outcomes.

Win = in stages 3-4: lower summarizer e2e/TTFT + fewer failures in A at the
same offered RPS, with the big GPU's tensor-active going idle→busy.

## Parallel two-harness runs (summarizer + planner)

The full benchmark runs two workloads concurrently against one stack: a
high-rate summarizer offering both models, and a low-rate "planner" pinned to
one model. Two `llmdbenchmark run` invocations **cannot share a namespace**
(each run's cleanup deletes prior harness pods), so the planner runs from a
second namespace in run-only mode, targeting the gateway by FQDN:

```bash
kubectl create namespace llmdbench-planner
# Terminal 1 — planner (second namespace, run-only mode; provisions its own
# harness PVC/pods there; -m pins the treatment to one model):
llmdbenchmark --spec cicd/kind-sim-multi run -l inference-perf \
  -w spike_planner.yaml -p llmdbench-planner -m facebook/opt-125m \
  -U http://infra-llmdbench-inference-gateway-istio.llmdbench.svc.cluster.local:80
# Terminal 2 (~60s later) — summarizer (stack namespace, as usual):
llmdbenchmark --spec cicd/kind-sim-multi run -l inference-perf \
  -w spike_summarizer.yaml -m facebook/opt-350m
```

Spike results (2026-06-11, validated on Kind):

- **Mechanics work**: run-only mode (`-U`) provisions harness infra in the
  second namespace; the two runs' cleanup steps are namespace-scoped and do
  not interfere; results collect independently.
- **Pinning proof**: in the planner-only windows (before/after the summarizer)
  IPP selections were 100% the pinned model; the planner never touched the
  other pool.
- **In-flight counter leak (scorer bug, found via this spike)**:
  `request-metadata-extractor` increments `Requests` per request but
  decrements only on *response events* (`requestmetadata/plugin.go:195` vs
  `:208`). Failed requests (Envoy 504s, shed 503s) never produce a response
  event, so a failure storm leaves the counter stuck high → `idleness ≈ 0` →
  the avg-ttft staleness decay never engages → the model's saturated EMA is
  frozen and it loses scoring forever (until an IPP restart). Observed: after
  the hot sweep, all summarizer traffic locked onto the 0.8 RPS sim
  (summarizer treatment 1: 78 ok / 282 fail, TTFT p90 18s) while the healthy
  fast sim idled; after `kubectl rollout restart` wiped the state, the same
  probe routed 9/10 to the fast sim (treatment 2: 360/360 ok, TTFT p90 1.0s).
  **Bring upstream** (relates to IPP PR #37 staleness-decay work) — failed
  requests must decrement the in-flight counter (or carry a timeout-based
  correction).

---

**Troubleshooting, required fixes, and findings → [`AGENTS.md`](./AGENTS.md).**

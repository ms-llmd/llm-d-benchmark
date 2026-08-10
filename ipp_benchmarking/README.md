# IPP benchmarking

Measure how IPP routing changes latency and throughput: a load-aware **smart**
scorer vs. a static or random baseline. Four experiments below.

This uses `llmdbenchmark` (the repo this lives in). Detailed instructions on how to use that are in the main [README](../README.md) of the repo.

Run `llmdbenchmark` **from the repo root** — it auto-discovers this bundle's
scenarios and specs. The workload profiles live in `workload/profiles/` at the
repo root and resolve by bare name (`-w`). Plotters need matplotlib, which is not
an `llmdbenchmark` dependency: `.venv/bin/pip install matplotlib` once, then run
them with `.venv/bin/python3`.

`IPP_PATH` must point at an IPP checkout that has the scorer plugins the arm's
values file uses, and whose chart still takes `payloadProcessor.listModels`
(upstream `main` renamed it, so `models.json` is never rendered and
`model-config-datasource` fails to start). Each values file pins the image
`tag` naming the build it needs; a checkout missing that plugin crashloops with
`plugin type '<name>' is not registered`. PR188 provides the `queue-ttft-scorer`
builds (`ttft-scorer`, §1 and §2); the `ttft-aware-scorer` /
`auto-group-model-name-filter` builds used by §3 and §4 (`ttft-aware-p25-expl`)
come from their own branches.

The run drivers in `tools/` require `NAMESPACE` to be exported and source an
optional gitignored `.env` from the repo root (put `HF_TOKEN` there). Nothing is
pinned to a machine, cluster or GPU model: paths derive from the script location
and the GPU node label is auto-detected.

Troubleshooting, gotchas and findings live in [`AGENTS.md`](./AGENTS.md).

## Why multi-stack scenarios set `secretName` themselves

The llm-d-router chart reads the EPP metrics-reader Secret name from
`router.monitoring.prometheus.auth.secretName`
(`charts/router/templates/_sa-token-secret.yaml`), but the repo's per-stack auto-suffix
(`render_plans.py` `_STACK_SCOPED_DEFAULTS`) rewrites `router.monitoring.secretName` —
a key the chart never reads. Every router in a namespace therefore falls back to the
chart default, and with one router Helm release per stack the second install fails:

```
Secret "inference-gateway-sa-metrics-reader-secret" ... cannot be imported into the
current release: annotation validation error: key "meta.helm.sh/release-name" must equal ...
```

So every multi-stack scenario here sets the name per stack on **both** keys —
`prometheus.auth.secretName` for the chart, and `monitoring.secretName` for
`05_namespace_sa_rbac_secret.yaml.j2`, whose RBAC `resourceNames` reads the second one and
would otherwise scope `collect_metrics.sh` to a Secret that does not exist. Single-stack
scenarios, and Kind (`prometheus.enabled: false`), are unaffected.

## Run data

`example_outputs/` is the directory results land in (gitignored — the run drivers
create it, so a fresh clone has nothing to plot yet). There is one directory per arm:
`example_outputs/<experiment>/<arm>/`. Plotters take arms as positional
`label=dir` plus `-o out.png`, and read:

| file | produced by |
|---|---|
| `stage_<N>_lifecycle_metrics.json`, `summary_lifecycle_metrics.json` (summary of the runs) | `llmdbenchmark run` |
| `harness_stdout.log` (stage bands) | `llmdbenchmark run` |
| `per_request_slim.json` (per request data, like TTFT and ITL) | `tools/extract_per_request_slim.py` |
| `ipp-full-live.log` (the ipp logs) | `tools/ab_routing_run.sh` |

`ab_routing_run.sh` and `adaptive_toggle_run.sh` run the expirements and write this layout themselves.

---

## 1. Kind simulator (no GPU)

This expirement uses Kind with [vllm simulators ](https://github.com/llm-d/llm-d-inference-sim).
We use two fake-latency simulators — `facebook/opt-125m` (slow) and `opt-350m` (fast) — for a
fast local smoke test of routing. A/B the scorer by re-running with
`maxscore-baseline-values.yaml` (no scorer → random ~50/50) as the second arm.

```bash
kind create cluster
docker pull ghcr.io/llm-d/llm-d-benchmark:v0.7.0 && kind load docker-image ghcr.io/llm-d/llm-d-benchmark:v0.7.0
export IPP_PATH=/path/to/llm-d-inference-payload-processor
make -C "$IPP_PATH" image-build REGISTRY=ghcr.io/<you> VERSION=ttft-scorer
kind load docker-image ghcr.io/<you>/llm-d-inference-payload-processor:ttft-scorer

./install.sh && source .venv/bin/activate
llmdbenchmark --spec cicd/kind-sim-multi standup -p llmdbench

# values file carries the plugin pipeline and listModels; it pins only the image
# TAG, so supply your own registry (the values files ship no registry on purpose)
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n llmdbench -f ipp_benchmarking/ipp_configs/kind-sim-smart-values.yaml \
  --set payloadProcessor.image.registry=ghcr.io/<you>
kubectl rollout restart deploy/payload-processor -n llmdbench   # config-only upgrades don't restart it
kubectl apply -n llmdbench -f ipp_benchmarking/ipp_configs/opt-125m-base-model.yaml \
                           -f ipp_benchmarking/ipp_configs/opt-350m-base-model.yaml

# header-match routes (the scenario disables the default PathPrefix route)
ipp_benchmarking/tools/gen_httproutes.sh llmdbench infra-llmdbench-inference-gateway \
  facebook/opt-125m facebook/opt-350m | kubectl apply -f -

# give the sims different TTFT/ITL so routing has something to optimize; not in the
# scenario, so re-apply after every standup
for d in $(kubectl get deploy -n llmdbench -o name | grep decode); do
  m=$(kubectl get $d -n llmdbench -o jsonpath='{.spec.template.spec.containers[0].args[1]}')
  case $m in *125m*) t=3s i=200ms;; *) t=1s i=50ms;; esac
  kubectl patch $d -n llmdbench --type=json -p="[{\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/args\",\"value\":[\"--model\",\"$m\",\"--port\",\"8200\",\"--served-model-name\",\"$m\",\"--time-to-first-token=$t\",\"--inter-token-latency=$i\",\"--max-num-seqs=10\"]}]"
done

llmdbenchmark --spec cicd/kind-sim-multi run -l inference-perf -w sanity_random.yaml
NAMESPACE=llmdbench ./ipp_benchmarking/collect_logs.sh        # -> ./collected-logs-<N>/

llmdbenchmark --spec cicd/kind-sim-multi teardown -p llmdbench
helm uninstall payload-processor -n llmdbench                 # IPP isn't torn down automatically
```

**Plot** — the shared RPS plotters read the requested rate from each stage file,
so they work on a Kind run unchanged. Arms are `label=dir`, where dir is a single
run's results dir; inside a `collected-logs-<N>/` bundle those sit under
`benchmark-results/results/` (one per stack, in scenario order):

```bash
R=(collected-logs-<N>/benchmark-results/results/*/)
.venv/bin/python3 ipp_benchmarking/tools/plot_ttft_per_stage.py \
  "opt-125m=${R[0]}" "opt-350m=${R[1]}" -o latency_per_stage_kind.png

# routing split (smart arm only -- the baseline has no scorer decisions)
.venv/bin/python3 ipp_benchmarking/tools/analyze_routing.py \
  collected-logs-<N>/payload-processor-*.log
```

---

## 2. OCP Qwen3-8B + 32B — smart routing vs static baselines

A deep-research agent hammers the fast 8B and leaves the 32B mostly idle. Smart
routing spills 8B overflow onto the 32B as load climbs.

Three runs, one arm per `ab_routing_run.sh` invocation — it patches the arm's values into the IPP, restarts it, runs the profile, streams the full IPP log to `ipp-full-live.log` (the container log rotates away under load), then collects. Standup/teardown use `cicd/ocp-qwen3-8b-32b` (both pools); the driver *runs* with `cicd/ocp-qwen3-8b-32b-summarizer`, a single-harness variant, so load has one unconfounded source.

All three profiles are open-loop **Poisson RPS** ladders, 11 stages × 300s (~55
min per arm), same request shape (~2048 in / ~256 out):

- **`static_8b` / `static_32b`** — the values file registers a single model, so
  there is no routing choice. `half_8b_poisson.yaml` runs 2.5→15→2.5 RPS and
  `half_32b_poisson.yaml` 1→6→1 RPS, so per stage `8B + 32B` sums to the smart
  arm's rate.
- **`smart`** — both models registered, no `auto-group-model-name-filter`, so
  candidates come from `listModels` and `queue-ttft-scorer` (+
  `ttft-percentile-extractor`, `explorationRate: 0.1`) with `max-score-picker`
  choose per request. `sweep_8stage_poisson.yaml` runs the summed ladder
  3.5→21→3.5 RPS. Needs an image built with `queue-ttft-scorer`. `avgttft-ocp-values.yaml`
  is the same scorer on the reduced-log `tracechunk` image; to use it drop the
  `image.tag` `--set` below, which would otherwise override the tag it pins.

All timeouts are lifted to 1200s so nothing is shed — the delta is latency and
throughput, not failures.

```bash
# Prereqs: OCP w/ H100-80GB, `oc login`, HF_TOKEN with Qwen access, IPP image pullable.
export NAMESPACE=llm-d-<you>
export IPP_PATH=/path/to/llm-d-inference-payload-processor

# 1. Standup both pools, then the two required decode fixes (crashloop otherwise).
llmdbenchmark --spec cicd/ocp-qwen3-8b-32b standup -p "$NAMESPACE"
for d in $(oc get deploy -n "$NAMESPACE" -o name | grep decode); do
  oc patch "$d" -n "$NAMESPACE" -p '{"spec":{"strategy":{"type":"Recreate","rollingUpdate":null}}}'
  oc set env "$d" -n "$NAMESPACE" -c vllm USER=vllm LOGNAME=vllm
  oc patch "$d" -n "$NAMESPACE" --type=json \
    -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--max-model-len=8192"}]'
done

# 2. Install IPP. VALUES selects which model(s) are REGISTERED -- re-run this same
#    upgrade with the matching values file before each arm below.
export VALUES=ipp_benchmarking/ipp_configs/static-8b-only-values.yaml
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n "$NAMESPACE" --set provider.name=istio -f "$VALUES" \
  --set provider.supportedEvents.requestBody=true --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseBody=true --set 'payloadProcessor.flags.v=4' \
  --set payloadProcessor.image.registry=ghcr.io/<you> --set payloadProcessor.image.tag=ttft-scorer \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway --set provider.messageTimeout=1200s
oc apply -n "$NAMESPACE" -f ipp_benchmarking/ipp_configs/qwen3-8b-base-model.yaml \
                         -f ipp_benchmarking/ipp_configs/qwen3-32b-base-model.yaml

# 3. Header-match routes (the scenario disables the default PathPrefix route).
ipp_benchmarking/tools/gen_httproutes.sh "$NAMESPACE" | oc apply -f -

# 4. Run ONE arm at a time (NAMESPACE is read from the env), re-running step 2 with
#    the matching values file before each.
ipp_benchmarking/tools/ab_routing_run.sh static_8b \
    ipp_benchmarking/ipp_configs/static-8b-only-values.yaml half_8b_poisson.yaml
ipp_benchmarking/tools/ab_routing_run.sh static_32b \
    ipp_benchmarking/ipp_configs/static-32b-only-values.yaml half_32b_poisson.yaml
ipp_benchmarking/tools/ab_routing_run.sh smart \
    ipp_benchmarking/ipp_configs/median-ttft-ocp-values.yaml sweep_8stage_poisson.yaml

llmdbenchmark --spec cicd/ocp-qwen3-8b-32b teardown -p "$NAMESPACE"
helm uninstall payload-processor -n "$NAMESPACE"
```

**Plot.** `plot_ttft_per_stage.py` reads each stage's requested RPS from the
stage file and overlays the arms by stage, printing the per-stage % change.
The arms are comparable **stage for stage** (`8B stage N + 32B stage N = smart
stage N`), not rate for rate — and the x labels come from the **first** arm
listed, so put `smart` first and read the static curves as its two halves.

```bash
D=ipp_benchmarking/example_outputs/ocp-research-agent-routing
RPS=3.5,7,10.5,14,17.5,21,17.5,14,10.5,7,3.5      # smart ladder; static legs are its halves
P=.venv/bin/python3

# per-stage latency vs requested RPS -- TTFT (default) and e2e
$P ipp_benchmarking/tools/plot_ttft_per_stage.py "smart"=$D/smart "static_8b"=$D/static_8b \
  -o $D/ttft_per_stage_ab.png
$P ipp_benchmarking/tools/plot_ttft_per_stage.py "smart"=$D/smart "static_8b"=$D/static_8b \
  --metric e2e -o $D/e2e_per_stage_ab.png

# 8B/32B split per stage from the IPP decision log
$P ipp_benchmarking/tools/analyze_routing.py $D/smart/ipp-full-live.log

# predicted-vs-actual TTFT for one arm; also writes a zoomed PNG per stage into <out>_stages/
$P ipp_benchmarking/tools/plot_ttft_actual_vs_predicted.py "smart"=$D/smart/ipp-full-live.log \
  --unit RPS --concurrencies $RPS -o $D/ttft_smart_fullrun.png
```

---

## 3. OCP Gemma-4-26B vs Qwen3.6-35B — adaptive routing

Two comparable FP8 MoE models (`RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic`,
`Qwen/Qwen3.6-35B-A3B-FP8`), 1× H100 each. A shared `model: "auto"` stream is
routed by `ttft-aware-scorer` to whichever pool has spare capacity; dedicated
per-model load is toggled on and off to show the shift **and the recovery**.

All three streams are the same synthetic summarization shape (~2048 in / ~256
out) at the same rate — only the `model` field differs, so a shared request and a
pinned request are the same unit of work. `auto-group-model-name-filter` reads it:
`"auto"` keeps both pools as candidates, an exact model name pins to one pool.

Poisson RPS per 300s stage, ~60 min total (36,000 requests):

| Stage        | Shared | Gemma-only | Qwen-only | Shows                     |
|--------------|--------|------------|-----------|---------------------------|
| -1 baseline1 | off    | 10         | 10        | baseline with no shared   |
|  0 baseline2 | 10     | off        | off       | reference split           |
|  1 pin Gemma | 10     | 10         | off       | shared shifts to Qwen     |
|  2 release   | 10     | off        | off       | recovery                  |
|  3 pin Qwen  | 10     | off        | 10        | shared shifts to Gemma    |
|  4 both      | 10     | 10         | 10        | overload (30 rps offered) |
|  5 release   | 10     | off        | off       | full recovery             |

Each pool sits just under saturation on its own; **stage 4 is deliberately
oversubscribed** (30 rps against ~25 rps of capacity) and tests routing under
overload, not steady state. Ablation arm: the same stages with
`adaptive-gemma-qwen-random-values.yaml` (identical pipeline minus the scorer, so
every `"auto"` request is a coin flip).

```bash
export NAMESPACE=<your-namespace>
export IPP_PATH=<path to a llm-d-inference-payload-processor checkout>   # helm chart only

# 1. Standup both pools (2 GPUs). Long download + ~5-min Qwen engine init.
llmdbenchmark --spec cicd/ocp-gemma-qwen-adaptive standup -p "$NAMESPACE"

# 2. Install the IPP. Image tag is MUTABLE -- the rollout restart is what re-pulls it.
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n "$NAMESPACE" --set provider.name=istio \
  -f ipp_benchmarking/ipp_configs/adaptive-gemma-qwen-smart-values.yaml \
  --set provider.supportedEvents.requestBody=true --set provider.supportedEvents.requestTrailers=true \
  --set provider.supportedEvents.responseBody=true --set 'payloadProcessor.flags.v=4' \
  --set payloadProcessor.image.registry=ghcr.io/<you> \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway --set provider.messageTimeout=1200s
oc rollout restart deploy/payload-processor -n "$NAMESPACE"
oc rollout status  deploy/payload-processor -n "$NAMESPACE" --timeout=180s

# 3. Base-model ConfigMaps (feed X-Gateway-Base-Model-Name) + header-match routes.
oc apply -n "$NAMESPACE" -f ipp_benchmarking/ipp_configs/gemma-26b-base-model.yaml \
                         -f ipp_benchmarking/ipp_configs/qwen36-35b-base-model.yaml
ipp_benchmarking/tools/gen_httproutes.sh "$NAMESPACE" infra-llmdbench-inference-gateway \
  RedHatAI/gemma-4-26B-A4B-it-FP8-dynamic Qwen/Qwen3.6-35B-A3B-FP8 | oc apply -f -

# 4. IPP capture must run IN-CLUSTER before the timeline starts (a laptop-side
#    `oc logs -f` loses most lines under load). Truncate the previous capture first.
oc apply -n "$NAMESPACE" -f ipp_benchmarking/ipp_configs/ipp-logtail-pod.yaml
oc exec -n "$NAMESPACE" access-to-harness-data-workload-pvc -- \
  truncate -s 0 /requests/smart-logs/ipp-full-live.log
oc get pod ipp-logtail -n "$NAMESPACE"        # must be Running

# 5. Run the 7-stage timeline and slice each stage window out of the capture.
#    Stages are discrete: each launches its own 300s runs and waits for all of them.
ipp_benchmarking/tools/adaptive_toggle_run.sh adaptive

llmdbenchmark --spec cicd/ocp-gemma-qwen-adaptive teardown -p "$NAMESPACE"
helm uninstall payload-processor -n "$NAMESPACE"
```

**Plot** — per-stage figures and tables (shared-probe percentiles per pool,
routing share over time):

```bash
.venv/bin/python3 ipp_benchmarking/tools/plot_adaptive_stages.py \
  ipp_benchmarking/example_outputs/gemma-qwen-adaptive/stage-by-stage-final -o /tmp/adaptive
```

### Files

| Purpose | Path |
|---|---|
| Scenario / spec | `config/scenarios/cicd/ocp-gemma-qwen-adaptive.yaml` (+ `config/specification/…`) |
| IPP values | `ipp_benchmarking/ipp_configs/adaptive-gemma-qwen-{smart,random}-values.yaml` |
| Base-model ConfigMaps | `ipp_benchmarking/ipp_configs/{gemma-26b,qwen36-35b}-base-model.yaml` |
| Workloads | `workload/profiles/inference-perf/adaptive_{shared,gemma,qwen}_summarization.yaml.in` |
| Run driver / single stage | `ipp_benchmarking/tools/adaptive_toggle_run.sh`, `tools/stage.sh` |
| Stage extractor, figures | `ipp_benchmarking/tools/ipp_extract_stage.sh`, `tools/plot_adaptive_stages.py` |

---

## 4. OCP dual-pool Qwen3-8B — weighted vs adaptive routing

The **same** model in two InferencePools with deliberately unequal capacity
(A = 2 pods, B = 3 pods). One harness, one gateway; only the HTTPRoute changes
per arm, so the arms differ solely in how load is split.

Two pools of one model need distinct `model.name` **aliases** (`-a` / `-b`) —
every resource name derives from `sha256(namespace/model.name)`, so identical
names collide. `huggingfaceId` stays the real id (weights load), and
`--served-model-name` lists all three names so either pool answers to any of
them. This requires dropping `modelCommand: imageDefault`, which silently
ignores `additionalFlags`; that also drops the image's `USER`/`LOGNAME`, so the
scenario sets them via `decode.extraEnvVars` or vLLM crashloops on `getpwuid`.
`modelPvc` must be RWX — RWO multi-attach-fails with >1 pod per pool.

Three arms over one standup, `poisson_rps_pyramid.yaml` (~75k requests each):

- **`w5050`** — weighted 50/50, capacity-blind.
- **`w4060`** — weighted 40/60, matching the 2:3 pod ratio.
- **`smart`** — IPP `ttft-aware-scorer` + `max-score-picker`. No ResponseProcessor
  is configured, so on builds whose streaming path skips the datalayer
  ResponseEvent no TTFT is recorded (`RecentN=0`) and routing is blind — check
  `RecentN` before trusting a smart-arm result.

```bash
export NAMESPACE=<your-namespace>
export IPP_PATH=/path/to/llm-d-inference-payload-processor

llmdbenchmark --spec cicd/ocp-qwen3-8b-dual-pool standup -p "$NAMESPACE"

# VALUES per arm: weighted arms use header plugins only (no model-selector);
# the smart arm adds the scorer, needs models.json re-injected after upgrade (recipe
# in AGENTS.md, "Upstream main's chart has no listModels"), and
# needs --set payloadProcessor.image.registry=ghcr.io/<you> (it pins only a tag).
helm upgrade --install payload-processor "$IPP_PATH/config/charts/payload-processor/" \
  -n "$NAMESPACE" -f ipp_benchmarking/ipp_configs/dual-pool-weighted-values.yaml \
  --set provider.name=istio --set provider.messageTimeout=1200s \
  --set inferenceGateway.name=infra-llmdbench-inference-gateway
oc rollout restart deploy/payload-processor -n "$NAMESPACE"
oc apply -n "$NAMESPACE" -f ipp_benchmarking/ipp_configs/qwen3-8b-a-base-model.yaml \
                         -f ipp_benchmarking/ipp_configs/qwen3-8b-b-base-model.yaml

# Verify the split before a real run. Freshly-scaled decode pods need ~2 min of
# EPP scrape lag before the pool reports endpoints -- until then every request is
# a 503 and "total served: 0". Re-run until it serves; the ratio is only
# indicative at n=20 (weighted routing is per-request random, so 1:1 commonly
# lands anywhere from 30/70 to 70/30).
NAMESPACE=$NAMESPACE ipp_benchmarking/tools/smoke_route_split.sh 20 1 1

# one arm per invocation; re-run the helm upgrade with the matching values first
ipp_benchmarking/tools/ab_pool_split_run.sh fixed_5050_poisson w5050 poisson_rps_pyramid.yaml
ipp_benchmarking/tools/ab_pool_split_run.sh fixed_4060_poisson w4060 poisson_rps_pyramid.yaml
ipp_benchmarking/tools/ab_pool_split_run.sh smart_poisson      smart poisson_rps_pyramid.yaml

llmdbenchmark --spec cicd/ocp-qwen3-8b-dual-pool teardown -p "$NAMESPACE"
helm uninstall payload-processor -n "$NAMESPACE"
```

**Plot.** Compare on mean/p99, not p50: 50/50 overloads the small pool while the
big one idles, so its median sits in the fast half and hides the tail.

```bash
D=ipp_benchmarking/example_outputs/ocp-qwen3-8b-dual-pool
A=("50/50=$D/fixed_5050_poisson" "40/60=$D/fixed_4060_poisson" "smart=$D/smart_poisson")
P=.venv/bin/python3

$P ipp_benchmarking/tools/plot_e2e_aggregate.py "${A[@]}" -o $D/e2e_aggregate_3way.png
$P ipp_benchmarking/tools/plot_throughput.py "${A[@]}" --mode perstage -o $D/throughput_perstage_3way.png
$P ipp_benchmarking/tools/plot_ttft_per_stage.py "${A[@]}" --metric e2e -o $D/e2e_perstage_3way.png
```

Result: 40/60 ≈ smart, both ≫ 50/50 (p99 54.7 / 58.2 / 99.5 s, peak throughput
55.9 / 55.3 / 45.7 req/s). Smart's value is finding the balance without knowing
the 2:3 ratio in advance.

### Files

| Purpose | Path |
|---|---|
| Scenario / spec | `config/scenarios/cicd/ocp-qwen3-8b-dual-pool.yaml` (+ `-run` for the harness, + `config/specification/…`) |
| IPP values | `ipp_benchmarking/ipp_configs/dual-pool-{weighted,smart}-values.yaml` |
| Base-model ConfigMaps | `ipp_benchmarking/ipp_configs/qwen3-8b-{a,b}-base-model.yaml` |
| Workloads | `workload/profiles/inference-perf/poisson_rps_pyramid.yaml.in` |
| Run driver | `ipp_benchmarking/tools/ab_pool_split_run.sh` |
| Weighted route, split check | `ipp_benchmarking/tools/gen_weighted_route.sh`, `tools/smoke_route_split.sh` |
| Figures | `ipp_benchmarking/tools/plot_e2e_aggregate.py`, `tools/plot_throughput.py` |

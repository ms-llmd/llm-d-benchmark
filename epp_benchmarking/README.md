# EPP multi-cluster benchmarking

Measure a router EPP whose candidate endpoints are whole **clusters** rather than
pods. Peer clusters are simulated with one namespace per cluster: each leaf
namespace runs a normal llm-d stack, and the router runs the standalone
(`epponly`) topology, so its Envoy forwards to whatever `IP:port` the EPP picks.

Troubleshooting and gotchas are in
[`troubleshooting_gotchas.md`](./troubleshooting_gotchas.md); design reasoning
and workload sizing are in [`CLAUDE.md`](./CLAUDE.md).

**Prerequisites:** `llmdbenchmark` on your `PATH` (see
[Getting Started → Install](../README.md#install)), plus `helm`, `envsubst`
(GNU gettext), `matplotlib` for the plots, and `kind` + `docker` for experiment 1.
All commands run from the repo root against your current kubeconfig.

## The experiments

| # | Experiment | Asks | Where | Cost |
|---|---|---|---|---|
| 1 | [Kind routing split](#1-kind--routing-split-on-simulators) | does the router follow capacity? | Kind, no GPU | ~10 min/arm |
| 2 | [Asymmetric capacity 2+3](#2-openshift--asymmetric-capacity-23) | does it follow a *subtle* capacity ratio, under real load? | OpenShift, 5 GPUs | ~25 min/arm |
| 3 | [TTFT prediction accuracy](#3-openshift--ttft-prediction-accuracy) | is the predicted TTFT actually right? | OpenShift, 1 GPU | ~35 min |

The routing experiments run **one model (Qwen3-8B, or simulators on Kind) in two
namespaces** — the namespaces are what stand in for separate clusters. Only the
capacity layout and the workload change. Experiment 3 is the exception: it uses
a single "leaf cluster", because it measures the scorer's model rather than its choices.

Run experiments 1 and 2 once per **arm** (routing policy) and compare the arms.

## Shared setup

### Router arms

Three policies live in [`router/values.yaml`](./router/values.yaml); pick one per
install with `--set router.epp.pluginsConfigFile=<file>`.

| arm | file | routes on |
|---|---|---|
| queue+KV | `mc-smart.yaml` | each leaf's *reported* queue depth and KV utilisation |
| TTFT-aware | `mc-ttft.yaml` | TTFT the router *measured itself*, per leaf, at its current in-flight count |
| random | `mc-random.yaml` | nothing — the ~50/50 baseline |

The chart's default EPP image has no multicluster plugins. `values.yaml` pins
`ghcr.io/llm-d/llm-d-router-endpoint-picker:main`, which has them; the TTFT arm
additionally needs the feature branch, published at
`ghcr.io/mohammad-nassar10/llm-d-router-endpoint-picker:dev`.

That image has *all* the plugins, so run **every** arm on it and the binary stops
being a variable:

```bash
--set router.epp.image.registry=ghcr.io/mohammad-nassar10 --set router.epp.image.tag=dev
```

The TTFT arm needs a **streaming** workload and a run long enough to outlast its
warm-up; both failure modes are silent (gotchas 43 and 49).

### Publish the cluster list

[`router/clusters.yaml`](./router/clusters.yaml) is what
`multicluster-file-discovery` reads. It is a template because ClusterIPs only
exist once the leaves do, and they must be IPs, not DNS names. **Run this after
standup, and again after any teardown/standup** — ClusterIPs do not survive.

```bash
export MC_A_NS=mc-a MC_B_NS=mc-b MC_ROUTER_NS=mc-router

gw_ip()   { kubectl -n "$1" get svc -l gateway.networking.k8s.io/gateway-name -o jsonpath='{.items[0].spec.clusterIP}'; }
gw_port() { kubectl -n "$1" get svc -l gateway.networking.k8s.io/gateway-name -o jsonpath='{.items[0].spec.ports[?(@.port==80)].port}'; }
epp_ip()  { kubectl -n "$1" get "$(kubectl -n "$1" get svc -o name | grep -m1 -- '-router-epp$')" -o jsonpath='{.spec.clusterIP}'; }

export MC_A_GW_IP=$(gw_ip "$MC_A_NS") MC_A_GW_PORT=$(gw_port "$MC_A_NS") MC_A_EPP_IP=$(epp_ip "$MC_A_NS")
export MC_B_GW_IP=$(gw_ip "$MC_B_NS") MC_B_GW_PORT=$(gw_port "$MC_B_NS") MC_B_EPP_IP=$(epp_ip "$MC_B_NS")

kubectl create ns "$MC_ROUTER_NS" --dry-run=client -o yaml | kubectl apply -f -
clusters=$(envsubst < epp_benchmarking/router/clusters.yaml)

if printf '%s' "$clusters" | grep -qE '(address|metricsAddress): *$|port: ""'; then
  echo 'ERROR: empty value -- are both leaves up in the namespaces you set?' >&2
else
  printf '%s' "$clusters" | kubectl -n "$MC_ROUTER_NS" create configmap mc-clusters \
    --from-file=clusters.yaml=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
fi
```

The `mc-clusters` name is the one name that is not free — `values.yaml` mounts it
by name. Everything else (`mc-a`, `mc-b`, the `mc-router` release) is yours to
rename consistently.

### Install the router

Add `-f epp_benchmarking/router/values-ocp.yaml` on GPU clusters — the Kind
values throttle the Envoy to one core. Add `--set router.epp.flags.v=4` if you
want the per-decision debug record.

```bash
helm upgrade --install mc-router \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v0.9.0 \
  -n "$MC_ROUTER_NS" -f epp_benchmarking/router/values.yaml \
  --set router.epp.pluginsConfigFile=mc-smart.yaml \
  --set router.epp.image.registry=ghcr.io/mohammad-nassar10 --set router.epp.image.tag=dev
kubectl -n "$MC_ROUTER_NS" rollout restart deploy/mc-router-epp
kubectl -n "$MC_ROUTER_NS" rollout status deploy/mc-router-epp --timeout=600s
```

To swap arms, re-run with a different `pluginsConfigFile` and restart. The
leaves and `clusters.yaml` are untouched. The `rollout restart` is not optional:
a config-only upgrade does not restart the pod.

On OpenShift, stand the **leaves up before the router** — the standalone chart
renders an InferencePool, and the `inference.networking.k8s.io` CRD arrives with
the first leaf.

## 1. Kind — routing split on simulators

Two leaves of deliberately unequal capacity (1 and 3 pods), same model, simulator
backends. A router that follows capacity should split ~25/75.

```bash
kind create cluster --name mc

for i in ghcr.io/llm-d/llm-d-benchmark:v0.7.0 \
         ghcr.io/llm-d/llm-d-inference-sim:v0.8.2 \
         ghcr.io/mohammad-nassar10/llm-d-router-endpoint-picker:dev; do
  docker pull "$i" && kind load docker-image "$i" --name mc
done

llmdbenchmark --spec cicd/kind-sim-mc-leaf standup -p mc-a --set decode.replicas=1
llmdbenchmark --spec cicd/kind-sim-mc-leaf standup -p mc-b --set decode.replicas=3
```

**Slow the sims down.** Without this they answer instantly, nothing queues, and
every arm scores identically.

```bash
for ns in mc-a mc-b; do
  d=$(kubectl -n $ns get deploy -o name | grep decode)
  kubectl -n $ns patch $d --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--model","facebook/opt-125m","--port","8200","--served-model-name","facebook/opt-125m","--time-to-first-token=2000","--inter-token-latency=100","--max-num-seqs=2"]}]'
done
```

Then publish the cluster list, install the router, and run one arm at a time:

```bash
epp_benchmarking/tools/analyze_split.py --save /tmp/before.json mc-a mc-b

llmdbenchmark --spec cicd/kind-sim-mc-leaf run -p mc-a -l inference-perf \
  -w mc_kind_poisson.yaml \
  --endpoint-url "http://mc-router-epp.$MC_ROUTER_NS.svc.cluster.local:8081"
```

## 2. OpenShift — asymmetric capacity 2+3

Experiment 1's question against real Qwen3-8B, one GPU per decode pod, two clusters of deliberately unequal size, **2 pods vs 3 pods**.

With 1+3 the right answer is 25/75 and a random picker's 50/50 is obviously
wrong. With 2+3 the right answer is **40/60** — close enough to even that a
router can look roughly correct by accident. So the split alone does not settle
it; read throughput and p90 latency alongside it.

```bash
export MC_A_NS=<prefix>-mc-a MC_B_NS=<prefix>-mc-b MC_ROUTER_NS=<prefix>-mc-router

llmdbenchmark --spec cicd/ocp-mc-leaf standup -p "$MC_A_NS" --set decode.replicas=2
llmdbenchmark --spec cicd/ocp-mc-leaf standup -p "$MC_B_NS" --set decode.replicas=3
```

Publish the cluster list, install the router (with `values-ocp.yaml`), then per
arm:

```bash
epp_benchmarking/tools/analyze_split.py --save /tmp/before.json "$MC_A_NS" "$MC_B_NS"

llmdbenchmark --spec cicd/ocp-mc-leaf run -p "$MC_A_NS" -l inference-perf \
  -w mc_asym_poisson.yaml \
  --endpoint-url "http://mc-router-epp.$MC_ROUTER_NS.svc.cluster.local:8081"
```

Per-stage TTFT percentiles across the arms come from `plot_stage_ttft.py` and
`plot_ttft_p95_timeseries.py`; see [Analysing results](#analysing-results).

## 3. OpenShift — TTFT prediction accuracy

The other experiments test what the router *did*. This tests whether the
TTFT-aware scorer's underlying model is *right* — it fits a piecewise-linear
curve of TTFT against an endpoint's in-flight count, and a scorer can rank two
endpoints correctly while that curve is badly wrong, which only breaks once the
endpoints stop resembling each other.

Both halves are in the router EPP's own `--v=4` log, joined on request id:

| line | field | is |
|---|---|---|
| `ttft-aware score` | `predictedTTFT` | what it expected, before dispatch |
| `ttft-observation` | `ttftSeconds` | what happened, at the first chunk |

So this needs **one** leaf, not two: point the router at a single endpoint with
[`router/clusters-solo.yaml`](./router/clusters-solo.yaml) instead of
`clusters.yaml`. The scorer still fits its curve and still logs a prediction for
every request; it just always picks the same endpoint, which gives that model
the whole ladder from idle to saturated rather than half of it.

```bash
# clusters-solo.yaml uses only the MC_A_* variables -- point them at the leaf
# you are calibrating, render, and apply as the mc-clusters ConfigMap.
# Then install the router on mc-ttft.yaml with --set router.epp.flags.v=4,
# start the logtail, and:
llmdbenchmark --spec cicd/ocp-mc-leaf run -p "$MC_A_NS" -l inference-perf \
  -w mc_ttft_calibration.yaml \
  --endpoint-url "http://mc-router-epp.$MC_ROUTER_NS.svc.cluster.local:8081"

epp_benchmarking/tools/collect_epp_log.sh "$MC_ROUTER_NS" /tmp/8b.epp.log
epp_benchmarking/tools/plot_ttft_accuracy.py /tmp/8b.epp.log --labels 8b -o accuracy.png
epp_benchmarking/tools/plot_ttft_timeseries.py /tmp/8b.epp.log \
  --results <results-dir> --per-stage -o timeseries.png
```

`plot_ttft_accuracy.py` gives a parity plot plus both series against in-flight
count — a predictor can sit on the parity line and still have the wrong slope,
and slope is what decides routing. `plot_ttft_timeseries.py` gives every
observation as a point with the prediction as a line; `--per-stage` additionally
writes one rescaled figure per rung, which is the only way to see the low rungs
at all once a single axis has been stretched by a 130 s excursion.

## Capturing the EPP log

Only needed if you want the per-decision record (`--set router.epp.flags.v=4`).
Reading it with `kubectl logs` loses most of a run; use
[`router/logtail.yaml`](./router/logtail.yaml), which follows the stream
in-cluster and filters before writing (gotcha 29).

```bash
kubectl apply -n "$MC_ROUTER_NS" -f epp_benchmarking/router/logtail.yaml
kubectl -n "$MC_ROUTER_NS" wait --for=condition=Ready pod/epp-logtail --timeout=180s

# after each arm -- drains the buffer and resets it, so arms stay separate
epp_benchmarking/tools/collect_epp_log.sh "$MC_ROUTER_NS" /tmp/epp-queue.log
```

It prints the distinct request IDs captured. **Check that against the requests
the ladder offered** (sum of rate × duration) — capture is not unconditionally
lossless, and line counts stay healthy while a partial capture happens
(gotcha 30).

## Analysing results

`tools/analyze_split.py` reports where traffic actually landed — counted from
each leaf EPP's own request histogram, not from what the router logged — plus the
latency and throughput the harness recorded. Counters are cumulative, hence the
`--save`/`--since` pair around each run. Metrics come through the API server's
service proxy, so no port-forward is needed.

```bash
epp_benchmarking/tools/analyze_split.py \
  --since /tmp/before.json --results <results-dir> "$MC_A_NS" "$MC_B_NS"
```

```
routed <n> requests
  mc-a     <n>   <pct>
  mc-b     <n>   <pct>

run summary (<treatment>)
  requests        <n> ok / <n> failed
  wall clock      <s>
  throughput      <req/s>, <out-tok/s>
  TTFT            mean <s>  p90 <s>
  latency         mean <s>  p90 <s>
```

### Latency over a ladder

**Experiment 1 only.** These read `per_request_lifecycle_metrics.json`, which the
GPU profiles switch off (gotcha 38). For experiments 2 and 3 use the per-stage
plots below.

`plot_e2e_timeseries.py` draws every request at its arrival time with a
continuous p50 line across all stages, labelling each stage band with its rate.
Extraction is separate because that file is multi-GB and usually truncated
mid-write; the extractor recovers every complete record instead of failing on
the tail.

```bash
run=<results-dir>/<run-subdir>
epp_benchmarking/tools/extract_per_request_slim.py \
  $run/per_request_lifecycle_metrics.json $run/per_request_slim.json
epp_benchmarking/tools/plot_e2e_timeseries.py $run --bin 15

# arms overlaid, one colour per run
epp_benchmarking/tools/plot_e2e_compare.py $smart_run $random_run \
  --labels scored,baseline --log
```

`plot_e2e_compare.py`'s x axis is stage progress, not elapsed time: an arm that
falls behind stretches its stages, so real time would slide identical rungs out
of alignment.

Colouring points by *leaf* is not possible from harness data — the per-request
records carry no upstream identity and both leaves answer to the same model name.
Per-leaf behaviour comes from the EPP gauges instead.

### TTFT percentiles per stage

Both take one directory per arm and write one figure per arm plus an aggregate.

```bash
epp_benchmarking/tools/plot_stage_ttft.py <queue-dir> <ttft-dir> <random-dir> \
  --labels queue,ttft,random --stat p95 --band p75,p99 -o outputs/asym

epp_benchmarking/tools/plot_ttft_p95_timeseries.py <queue-dir> <ttft-dir> <random-dir> \
  --labels queue,ttft,random --per-stage -o outputs/asym
```

`plot_stage_ttft.py` reads the percentile straight from each
`stage_*_lifecycle_metrics.json`, so it is exact and works for every arm.
x is stage in run order, not offered rate — the ladder comes back down, and
plotting against rate would fold the descent onto the ascent and hide the
hysteresis.

`plot_ttft_p95_timeseries.py` gives shape *within* a stage instead, differencing
vLLM's `time_to_first_token_seconds_bucket` scrapes. Two limits: resolution is
the ~16 s scrape interval and accuracy is bounded by vLLM's bucket edges (coarse
above 10 s), and llmdbenchmark only scrapes the namespace passed to `-p`, so it
covers that one leaf, not the fleet.

## Files

| path | what |
|---|---|
| `troubleshooting_gotchas.md` | 49 numbered gotchas, EPP and IPP, mostly OpenShift |
| `CLAUDE.md` | workload sizing and design reasoning |
| `router/clusters.yaml` | peer cluster list; `envsubst` template |
| `router/clusters-solo.yaml` | single-endpoint list, for experiment 3 |
| `router/values.yaml` | router EPP chart values; all three arms in `pluginsCustomConfig` |
| `router/values-ocp.yaml` | sizing overlay for GPU clusters |
| `router/logtail.yaml` | in-cluster router-EPP log capture that survives rotation |
| `tools/analyze_split.py` | routing split + run summary |
| `tools/collect_epp_log.sh` | drain the logtail buffer into a per-arm file |
| `tools/extract_per_request_slim.py` | slim per-request records from the multi-GB dump |
| `tools/plot_e2e_timeseries.py` | per-request latency scatter + p50 over a ladder |
| `tools/plot_e2e_compare.py` | the same, several runs overlaid, one colour per run |
| `tools/plot_stage_ttft.py` | exact TTFT percentile per stage, arms overlaid |
| `tools/plot_ttft_p95_timeseries.py` | TTFT percentile within a stage, from vLLM histograms |
| `tools/plot_ttft_accuracy.py` | predicted vs actual: parity + curve-vs-load |
| `tools/plot_ttft_timeseries.py` | every observation over time, prediction as a line |
| `config/scenarios/cicd/kind-sim-mc-leaf.yaml` | leaf stack (sim), one per namespace |
| `config/scenarios/cicd/ocp-mc-leaf.yaml` | leaf stack (Qwen3-8B, 1 GPU/pod) |
| `workload/.../mc_kind_poisson.yaml.in` | saturating ladder for the sims (experiment 1) |
| `workload/.../mc_asym_poisson.yaml.in` | 12→60 req/s ladder for the 2+3 layout (experiment 2) |
| `workload/.../mc_ttft_calibration.yaml.in` | 2→24 req/s single-leaf ladder (experiment 3) |

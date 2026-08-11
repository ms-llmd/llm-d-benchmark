# EPP multi-cluster benchmarking

Measure a router EPP whose candidate endpoints are whole clusters, scored on each
cluster's pool-aggregate KV-cache and queue metrics.

Peer clusters are simulated with one namespace per "cluster". Each leaf
namespace runs a normal llm-d stack; the router runs the standalone
(`epponly`) topology, so its Envoy forwards to whatever `IP:port` the EPP
picks.

```
load ──> router Envoy :8081 ──> router EPP (multicluster plugins)
                                  │ reads clusters.yaml (peer gateway ClusterIPs)
                                  │ scrapes each leaf EPP :9090/metrics
                                  └─> leaf gateway :80 ──> leaf EPP ──> vLLM
```

Troubleshooting and Gotchas are in [`CLAUDE.md`](./CLAUDE.md).

**Prerequisites:** `llmdbenchmark` installed and on your `PATH` — see
[Getting Started → Install](../README.md#install) in the repo README — plus
`kind`, `docker`, `helm`, and `envsubst` (from GNU gettext; not installed by
default on macOS). Every command below runs from the repo root against your
default kubeconfig.

## Kind (no GPU)

Two leaves with asymmetric capacity, same model, sim backends. `mc-a`, `mc-b`
and `mc-router` are just literals — rename them as long as you do so
consistently, remembering that the Helm release name determines the
`mc-router-epp` Deployment, Service and endpoint URL used later. The one name
that is *not* free is the `mc-clusters` ConfigMap: `router/values.yaml` mounts
it by name, so change it in both places or not at all.

### 1. Create the cluster and load images

Side-loading keeps standup from timing out on a cold image pull. The tags must
match what your `llmdbenchmark` version actually deploys — if standup still
stalls pulling, check `images.benchmark` in `config/templates/values/defaults.yaml`
and side-load that tag instead.

```bash
kind create cluster

for i in ghcr.io/llm-d/llm-d-benchmark:v0.7.0 \
         ghcr.io/llm-d/llm-d-inference-sim:v0.8.2 \
         ghcr.io/llm-d/llm-d-router-endpoint-picker:main; do
  docker pull "$i" && kind load docker-image "$i"
done
```

### 2. Stand up an llm-d stack in each namespace

After the images finished loading, we stand up one llm-d stack per namespace, asymmetric capacity so the router has something to decide.

```bash
llmdbenchmark --spec cicd/kind-sim-mc-leaf standup -p mc-a --set decode.replicas=1
llmdbenchmark --spec cicd/kind-sim-mc-leaf standup -p mc-b --set decode.replicas=3
```

### 3. Slow the sims down

Without this the sims answer instantly, nothing ever queues, and both arms score
identically — see "Load must saturate" in `CLAUDE.md`.

```bash
for ns in mc-a mc-b; do
  d=$(kubectl -n $ns get deploy -o name | grep decode)
  kubectl -n $ns patch $d --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/args","value":["--model","facebook/opt-125m","--port","8200","--served-model-name","facebook/opt-125m","--time-to-first-token=2000","--inter-token-latency=100","--max-num-seqs=2"]}]'
done
```

### 4. Publish the cluster list

[`router/clusters.yaml`](./router/clusters.yaml) is what `multicluster-file-discovery`
reads. It is a template because ClusterIPs are only known once the leaves exist,
and they must be IPs rather than DNS names.

```bash
export MC_A_NS=mc-a MC_B_NS=mc-b

gw_ip()   { kubectl -n "$1" get svc -l gateway.networking.k8s.io/gateway-name -o jsonpath='{.items[0].spec.clusterIP}'; }
gw_port() { kubectl -n "$1" get svc -l gateway.networking.k8s.io/gateway-name -o jsonpath='{.items[0].spec.ports[?(@.port==80)].port}'; }
epp_ip()  { kubectl -n "$1" get "$(kubectl -n "$1" get svc -o name | grep -m1 -- '-router-epp$')" -o jsonpath='{.spec.clusterIP}'; }

export MC_A_GW_IP=$(gw_ip "$MC_A_NS") MC_A_GW_PORT=$(gw_port "$MC_A_NS") MC_A_EPP_IP=$(epp_ip "$MC_A_NS")
export MC_B_GW_IP=$(gw_ip "$MC_B_NS") MC_B_GW_PORT=$(gw_port "$MC_B_NS") MC_B_EPP_IP=$(epp_ip "$MC_B_NS")

kubectl create ns mc-router

clusters=$(envsubst < epp_benchmarking/router/clusters.yaml)
printf '%s\n' "$clusters"

# Any lookup that missed renders as an empty value, and file discovery skips
# that endpoint silently -- leaving a router that scores one cluster. Refuse to
# apply instead.
if printf '%s' "$clusters" | grep -qE '(address|metricsAddress): *$|port: ""'; then
  echo 'ERROR: empty value above -- are both leaves up in the namespaces you set?' >&2
else
  printf '%s' "$clusters" | kubectl -n mc-router create configmap mc-clusters \
    --from-file=clusters.yaml=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -
fi
```

### 5. Install the router

The chart's default EPP image has no multicluster plugins;
[`router/values.yaml`](./router/values.yaml) pins a newer one that does, and
enables the Alpha plugin gate.

```bash
helm upgrade --install mc-router \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v0.9.0 \
  -n mc-router -f epp_benchmarking/router/values.yaml
kubectl -n mc-router rollout status deploy/mc-router-epp --timeout=300s
```

### 6. Benchmark through the router

`--endpoint-url` points the harness at the router instead of a single stack, so
the load runs in-cluster and every request is routed by the multicluster
scorers.

```bash
epp_benchmarking/tools/analyze_split.py --save /tmp/before.json mc-a mc-b

llmdbenchmark --spec cicd/kind-sim-mc-leaf run -p mc-a -l inference-perf \
  -w mc_kind_concurrent.yaml \
  --endpoint-url http://mc-router-epp.mc-router.svc.cluster.local:8081
```

To compare against the no-scoring baseline, swap the picker and repeat step 6.
`clusters.yaml` is unchanged.

```bash
helm upgrade --install mc-router \
  oci://ghcr.io/llm-d/charts/llm-d-router-standalone --version v0.9.0 \
  -n mc-router -f epp_benchmarking/router/values.yaml \
  --set router.epp.pluginsConfigFile=mc-random.yaml
kubectl -n mc-router rollout restart deploy/mc-router-epp
kubectl -n mc-router rollout status deploy/mc-router-epp --timeout=300s
```

## Analysing a run

`tools/analyze_split.py` reports where traffic actually landed — counted from
each leaf EPP's own request histogram, not from what the router logged — plus
the latency and throughput the harness recorded. Pass the results directory
`llmdbenchmark run` printed as `Local results:`.

```bash
epp_benchmarking/tools/analyze_split.py \
  --since /tmp/before.json \
  --results <results-dir> \
  mc-a mc-b
```

It prints the per-namespace request counts and shares, then the run's request
count, wall clock, throughput, and mean/p90 TTFT and latency:

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

Run it once per arm and compare: the scored arm should track the leaves' capacity
ratio while the `random-picker` baseline splits evenly.

The counters are cumulative, hence the `--save`/`--since` pair around each run.
Metrics are read through the API server's service proxy, so no port-forward is
needed.

## Files

| path | what |
|---|---|
| `CLAUDE.md` | gotchas, workload sizing, design reasoning |
| `router/clusters.yaml` | peer cluster list; `envsubst` template (step 4) |
| `router/values.yaml` | router EPP chart values; both arms in `pluginsCustomConfig` |
| `tools/analyze_split.py` | routing split + run summary |
| `config/scenarios/cicd/kind-sim-mc-leaf.yaml` | leaf stack, stood up once per namespace |
| `workload/profiles/inference-perf/mc_kind_concurrent.yaml.in` | saturating ladder for the sims |

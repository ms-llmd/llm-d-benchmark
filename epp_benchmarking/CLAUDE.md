# EPP multi-cluster benchmarking -- notes

Design reasoning and workload sizing for the cluster-scoped router EPP.
Troubleshooting and gotchas live in `troubleshooting_gotchas.md`.

## Never use closed-loop load

**Every profile is `load.type: poisson`. Do not write `type: concurrent` or
`concurrency_level`, anywhere, for any reason.**

A closed-loop client only sends a new request once an old one returns, so it
self-throttles and a backlog can never form. Queue depth and KV utilization --
the exact signals the multicluster scorers read -- then stay near zero however
slow a leaf gets, and every arm ties. Measured on H100s running Qwen3-8B: a
fixed-concurrency run at 256 with 512-token outputs peaked at **7% KV
utilization and zero queueing**. An 80GB card holds far more of an 8B model's
KV than a closed-loop harness ever offers it, because every request the server
has not answered is a request the harness has not sent.

Open-loop keeps offering requests at the stated rate, so a leaf that falls
behind accumulates backlog and becomes visible. This holds even when a
closed-loop sweep looks like the more direct probe of something load-dependent
(such as the TTFT curve, which is a function of in-flight count): it is not an
acceptable trade here.

## Sizing the workload

Getting this wrong is the next most likely reason a run produces a tie or fails
outright, and the right size is hardware-specific.

`mc_kind_poisson.yaml` is sized for the sims: deliberately slowed and capped at
`--max-num-seqs=2`, so a pod serves 2 at a time at ~5.2s each -- ~0.385 req/s
per pod, ~1.54 req/s for a 1+3 fleet, and the 1-pod leaf breaks first at ~0.77
req/s offered under an even split. Prompts must stay under the scenario's
`maxModelLen: 1024`, or every request 400s.

A GPU-sized ladder pointed at Kind sends thousands of requests at rates the sims
cannot approach, and the harness times out before finishing.

`mc_asym_poisson.yaml` targets the GPU leaves instead. The servers stay
unthrottled -- no `--max-num-seqs` cap, `gpuMemoryUtilization` at 0.90 -- so the
numbers remain a real capacity measurement.

The asymmetry is what makes the rates enough. Under an even split a 2+3 layout
puts half the fleet's rate on the 2-pod leaf, so it saturates before the fleet
does. The scored arm should close that gap; the `random-picker` arm cannot, and
its queue depth on the small leaf is the tell. Fleet-wide saturation is not
required and would mostly cost GPU hours.

The ladder climbs 12->60 req/s and back to 24, 180s per stage with `interval:
0`, so a stage's backlog carries into the next one. 45,360 requests, ~25 min per
arm. The descent is not decoration: a router that only sheds load one way looks
correct on the way up.

`total_count: 4000` means 4,000 distinct prompts are reused across those 45k
requests, so prefix-cache hits are common. That inflates throughput relative to
unique traffic, equally on both arms.

`per_request: false` is not optional at these rates: 45,360 records
OOM-killed the harness pod mid-write on the first attempt, leaving a 0-byte
`per_request_lifecycle_metrics.json`. The stage summaries survived, which is
what every plot here reads anyway.

## Load has to come from inside the cluster

Driving requests with `curl` through a `kubectl port-forward` cannot saturate
real GPUs -- the single proxied TCP connection, not the accelerator, becomes the
bottleneck. Use `llmdbenchmark run --endpoint-url <router>` so the harness pod
runs in-cluster.

## Why a namespace can stand in for a cluster

Every per-stack resource name derives from `sha256(namespace/model.name)`, so
two namespaces already yield distinct InferencePools, EPPs and Deployments for
the *same* `model.name`. None of the `-a`/`-b` model aliasing or
`--served-model-name` juggling that a two-pools-in-one-namespace layout needs
applies here.

The router uses the standalone (`epponly`) topology deliberately: its Envoy
routes purely off the destination header via `ORIGINAL_DST`, with no
InferencePool or EndpointSlice membership check, which is what lets it forward
to a gateway in another namespace at all.

## Environment notes

- `kubectl get --raw /api/v1/namespaces/<ns>/services/<svc>:9090/proxy/metrics`
  reads EPP metrics through the API server -- no port-forward, no free local
  port. `tools/analyze_split.py` uses this.
- On a cold Kind cluster the `llm-d-benchmark` image pull can exceed the 6-minute
  data-access wait. Side-load images with `kind load` first (step 1).

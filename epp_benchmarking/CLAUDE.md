# EPP multi-cluster benchmarking -- notes

Findings, gotchas and dead ends for the cluster-scoped router EPP. Every item
here was hit and verified against a running cluster.

## Gotchas

Each of these silently produces a no-op rather than an error, which is what
makes them expensive.

- **Leaf metrics auth.** The EPP metrics endpoint defaults to
  `--metrics-endpoint-auth=true` and answers **401** to an anonymous scrape.
  `multicluster-metrics-data-source` supports TLS client certs but *no bearer
  token*, so the scrape fails, no attribute is written, and both scorers return
  no score. The leaf scenarios set `router.monitoring.prometheus.auth.enabled:
  false`, which the chart renders to `--metrics-endpoint-auth=false`.

- **Scheme.** `multicluster-metrics-data-source` defaults to `https` (it is
  meant to cross a trust boundary). A leaf EPP serves metrics over plain HTTP
  unless `metrics-cert-dir` is set, so the router config pins `scheme: http`.

- **`schedulingProfiles` is not optional.** An empty list passes validation and
  yields a profile with no scorers -- the EPP starts happily and scores nothing.
  The upstream config example omits it, so copying that example gives you a
  router that looks healthy and routes at random.

- **`address` must be an IP.** The router's Envoy resolves the picked endpoint
  through `ORIGINAL_DST` with `use_http_header`, which parses
  `x-gateway-destination-endpoint` as `IP:port`. File discovery itself accepts
  hostnames, so a Service DNS name loads fine and then every request fails with
  **503 "no healthy upstream"** (verified). `metricsAddress` is scraped by the
  EPP's own Go client, so a DNS name *would* work there.

- **ClusterIPs do not survive teardown/standup.** They are stable for a
  Service's lifetime only. Re-render `clusters.yaml` after rebuilding a leaf, or
  the router points at an address that no longer answers.

- **Image.** `v0.9.0` predates the multicluster plugins and contains none of
  them -- confirmed by extracting the binary and grepping for the plugin type
  strings. Use `tag: main`. All six variants are registered **Alpha**, so
  `--allow-experimental-plugins` is required or the EPP refuses to start.

- **Load must saturate.** Both scorers read queue depth and KV utilization.
  Below saturation every cluster reports 0, `max-score-picker` ties, and the
  split looks random on *both* arms -- so a too-gentle workload silently
  produces a null result rather than a failure.

- **Second datalayer poller.** The EPP auto-instantiates the stock
  `metrics-data-source` / `core-metrics-extractor` alongside the multicluster
  pair. It scrapes the peer gateway port and logs failures. Harmless, but it is
  noise when reading router logs.

## Sizing the workload

Getting this wrong is the most likely reason a run produces a tie or fails
outright, and the right size is hardware-specific.

`mc_kind_concurrent.yaml` is sized for the sims: deliberately slowed and capped
at `--max-num-seqs=2`, so 1+3 pods saturate around 8 concurrent. Prompts must
stay under the scenario's `maxModelLen: 1024`, or every request 400s.

Porting this to accelerators needs a much heavier profile, and a much lighter
one is not safe either:

- Real GPUs are far harder to saturate than they look. On H100s running
  Qwen3-8B, concurrency 256 with 512-token outputs peaked at **7% KV
  utilization and zero queueing** -- no signal at all, so both arms tie.
- A GPU-sized ladder pointed at Kind sends thousands of requests at roughly
  3 req/s and the harness times out before finishing.

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

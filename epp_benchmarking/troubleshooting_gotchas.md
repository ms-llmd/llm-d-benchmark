# Troubleshooting and gotchas — OpenShift

Problems hit and verified against a running cluster while benchmarking the
multi-cluster router EPP, plus the ones carried over from `ipp_benchmarking`.
Source column: **E** = EPP (this directory), **I** = IPP, **E+I** = hit
independently in both.

Most of these produce a *healthy-looking pod that measures nothing* rather than
an error, which is what makes them expensive.

## 1. OpenShift platform

| # | Problem | Cause / fix | src |
|---|---|---|---|
| 1 | SELinux denies `/var/log/pods` even to a root container under `hostmount-anyuid` | The node-file log reader that works on Kind is unusable here; forced the in-cluster filtered-follow design in `router/logtail.yaml`. | E |
| 2 | Decode pods crash with `getpwuid(): uid not found` | OCP runs vLLM under an arbitrary high UID absent from `/etc/passwd`, so torch's `getpass.getuser()` dies. Set `USER`/`LOGNAME`. Patch `strategy: Recreate` first, or the rolling update's surge pod deadlocks waiting for a GPU that does not exist. | I |
| 3 | `gpu-reaper` scales idle GPU deployments to 0 after 90 min (pokprod001) | Annotation `gpu-reaper.io/reason: Idle since ...`. A pool left idle while you debug silently loses its pod and the next run sends traffic into a dead backend. Check replicas before every run; keep idle gaps under 90 min. | I |
| 4 | No integrated image registry — `oc new-build` fails with `InvalidOutputReference` | The EPP image cannot be built in-cluster. `oc new-build` also fails first with "No Dockerfile was found" because the repo has `Dockerfile.epp`. Use a pre-published image and verify it by extracting the binary and grepping for the plugin type strings. | E |
| 5 | RWO `workload-pvc` Multi-Attach | A harness pod on a different node than `access-to-harness-data-workload-pvc` hangs in `ContainerCreating` until the 3600 s timeout. A manually recreated helper then blocks the *next* run's step 02 (`Forbidden: pod updates may not change fields other than image`). Durable fix: RWX — `ibm-spectrum-scale-fileset` supports it. | I |
| 6 | Node without the storage CSI driver | `CSINode ... does not contain driver spectrumscale.csi.ibm.com`. Cordoning is cluster-scoped and may be disallowed; namespace-scoped fix is `oc annotate namespace <ns> openshift.io/node-selector="scale=true"` (revert after). | I |
| 7 | HuggingFace egress fails on some nodes | IPv4-only workers cannot reach HF, so tokenizer init fails; IPv6 nodes succeed. Re-running usually reschedules onto a working node. | I |
| 8 | `route already exists` during standup | Non-fatal leftover from a prior run; standup still completes. | I |

## 2. Standup and deployment

| # | Problem | Cause / fix | src |
|---|---|---|---|
| 9 | The router must go up **after** the leaves | The standalone chart renders an InferencePool, so `inference.networking.k8s.io` must already exist — that CRD arrives with the first leaf. | E |
| 10 | ClusterIPs do not survive teardown/standup | Stable for a Service's lifetime only. Re-render `clusters.yaml` after any rebuild, or the router points at an address that no longer answers. | E |
| 11 | Helm deep-merges the two values files | `-f values.yaml -f values-ocp.yaml` merges key by key, so a `limits` entry left out of the overlay silently keeps the Kind value — Envoy capped at 1 core while the memory limit reads 16Gi, which looks deliberate. Restate every field. | E |
| 12 | A config-only `helm upgrade` does not restart the pod | Neither chart has a ConfigMap checksum annotation. `rollout restart` between arms is mandatory, or you benchmark the previous arm twice. | E+I |
| 13 | `model.size` is the decode pod's emptyDir `sizeLimit` | Undersize it and the pod downloads weights, gets `Evicted` ("Usage of EmptyDir volume ... exceeds the limit"), and the replacement repeats forever. Qwen3-32B BF16 needs `70Gi`. Live fix: patch the deployment volume's `emptyDir.sizeLimit`. | I |
| 14 | Scenario `maxModelLen` is ignored, so Qwen3-32B crashloops | `modelCommand: imageDefault` only exports `VLLM_MAX_MODEL_LEN`, which that vLLM does not read. It needs 10 GiB KV for the 40960 default against ~8.25 available. Patch `--max-model-len 8192` on **both** decode deployments, or the comparison is unfair. | I |
| 15 | Two pools collide on the shared gateway secret in parallel | Stand them up sequentially with `LLMDBENCH_WAIT_TIMEOUT=6000`, and poll `/v1/completions` — not `/health` — for real vLLM readiness. | I |
| 16 | Multi-stack router installs fight over one Secret name | The chart reads `router.monitoring.prometheus.auth.secretName`, but the repo's per-stack auto-suffix rewrites `router.monitoring.secretName`, a key the chart never reads. The second install fails `meta.helm.sh/release-name` annotation validation. Set the name on **both** keys. | I |
| 17 | Model PVCs are not reliably preserved across teardown | Expect a fresh ~60-64 GB download per model. With `uriProtocol: hf` there is no model PVC at all — every replica downloads its own copy into a `model-storage` emptyDir. | I |

## 3. Configuration that silently no-ops

| # | Problem | Cause / fix | src |
|---|---|---|---|
| 18 | `schedulingProfiles` is not optional | An empty list passes validation and yields a profile with no scorers — the EPP starts happily and scores nothing. The upstream config example omits it, so copying that example gives a router that looks healthy and routes at random. | E |
| 19 | Leaf EPP metrics answer **401** to an anonymous scrape | The endpoint defaults to `--metrics-endpoint-auth=true`. `multicluster-metrics-data-source` supports TLS client certs but **no bearer token**, so the scrape fails, no attribute is written, and both scorers return no score. Leaf scenarios set `router.monitoring.prometheus.auth.enabled: false`. | E |
| 20 | `multicluster-metrics-data-source` defaults to `https` | A leaf EPP serves metrics over plain HTTP unless `metrics-cert-dir` is set. Pin `scheme: http`. | E |
| 21 | `address` must be an IP, not a DNS name | Envoy resolves the picked endpoint through `ORIGINAL_DST` with `use_http_header`, parsing `x-gateway-destination-endpoint` as `IP:port`. File discovery accepts hostnames, so a Service DNS name loads fine and then every request fails with **503 "no healthy upstream"**. `metricsAddress` is scraped by the EPP's own Go client, so a DNS name would work there. | E |
| 22 | Image `v0.9.0` contains none of the multicluster plugins | Confirmed by extracting the binary and grepping for the plugin type strings. Use `tag: main`. All variants register **Alpha**, so `--allow-experimental-plugins` is required or the EPP refuses to start. | E |
| 23 | `affinity.nodeSelector` is ignored unless `affinity.enabled` is also set | It defaults to `false`. Prefer `decode.acceleratorType.labelKey`/`labelValue`. | E |
| 24 | Supplying any `--plugin` replaces the entire default set | Include everything you need explicitly. A plugin that reads the body also sees nothing unless `provider.supportedEvents.requestBody`/`responseBody` is true. | I |
| 25 | `listModels` renders nothing on upstream `main`'s chart | IPP then crashloops on a missing `/config/models.json`. Use a chart that has the block, or inject the key into the ConfigMap. | I |
| 26 | `provider.messageTimeout=10s` is required | Envoy's ~200 ms default trips `HTTP 504 ext_proc_error_per-message_timeout_exceeded` once IPP defers header ACKs under load. | I |
| 27 | Base-model ConfigMaps must carry `inference.llm-d.ai/ipp-managed: "true"` | The default plugins ignore them otherwise. | I |
| 28 | No HTTPRoutes after standup | IPP routes by the `X-Gateway-Base-Model-Name` header, which a PathPrefix route cannot express, so the scenario sets `httpRoute.enabled: false` and the gateway 404s everything until header-match routes exist. | I |

## 4. Log capture and observability

| # | Problem | Cause / fix | src |
|---|---|---|---|
| 29 | `kubectl logs` loses most of a `--v=4` run | kubelet rotates the container log at 10Mi and `kubectl logs` reads only the current file, so counts derived from it go *down* as a run proceeds and `logs -f` exits at each rotation. Measured on one 400-request arm: laptop-side `kubectl logs` **250/400**; in-cluster `logs -f` with a `--since=5s` restart loop **28/400**; in-cluster follow filtered before writing **400/400**. | E+I |
| 30 | The logtail is **not** unconditionally lossless | 400/400 on Kind and 45,360/45,360 on the 2+3 arms, but 11,569/15,480 on the single-leaf calibration ladder — and the loss was not spread out: stages 0-5 were 100% and the final 24 req/s stage was **9.5%**. Line counts look healthy while this happens. Always compare captured request IDs against requests *offered* (sum of rate x duration), per stage if it matters. | E |
| 31 | `/debug/plugins/state` does not exist in file-discovery mode | The handler is mounted on the controller-runtime manager, which the multicluster router never starts; it serves a standalone mux with `/metrics` and pprof only. Any plugin's `DumpState` is unreachable — debug logs are the only window into scorer internals. | E |
| 32 | `oc cp` truncates | Use `oc rsync`. | I |
| 33 | GPU FLOPs are not in the harness metrics scrape | `metricsScrapeEnabled` scrapes vLLM and EPP `/metrics` only. FLOPs come from DCGM via Thanos. Use `DCGM_FI_PROF_PIPE_TENSOR_ACTIVE` — **not** `DCGM_FI_DEV_GPU_UTIL` (pinned ~100%, useless) nor `PIPE_FP16_ACTIVE` (~0, vLLM is BF16). | I |
| 34 | A second datalayer poller adds noise | The EPP auto-instantiates the stock `metrics-data-source` / `core-metrics-extractor` alongside the multicluster pair. It scrapes the peer gateway port and logs failures — harmless, but confusing when reading router logs. | E |
| 35 | `collect_logs.sh` finds no workspace | It greps `/tmp` at depth 1, but the workspace may live under `/tmp/<user-or-runtime>/`. Point `LLMDBENCH_WORKSPACE` at it explicitly. | I |

## 5. Measurement validity

These produce wrong numbers rather than errors.

| # | Problem | Cause / fix | src |
|---|---|---|---|
| 36 | Closed-loop load makes every arm tie | A concurrency client only sends a new request once an old one returns, so it self-throttles and a backlog can never form. Queue depth and KV utilization — the exact signals the scorers read — stay near zero however slow a leaf gets. Measured on H100s running Qwen3-8B: concurrency 256 with 512-token outputs peaked at **7% KV utilization and zero queueing**. Poisson RPS only. | E |
| 37 | Load must saturate | Below saturation every cluster reports 0, `max-score-picker` ties, and the split looks random on *both* arms — a too-gentle workload is a null result, not a failure. | E |
| 38 | `per_request: true` OOM-killed the harness at 45k requests | Exit 137, leaving a 0-byte `per_request_lifecycle_metrics.json`. The stage summaries survived, which is what the plots read anyway. | E |
| 39 | One inference-perf pod caps at ~130-155 req/s | It issues the full request count but stretches it over wall-clock, so a "500 RPS" label can be fiction. Verify `load_summary.count / benchmark_time_seconds` in `stage_*_lifecycle_metrics.json`. Use `-j N` parallel pods (~150 RPS each), which then needs RWX `workload-pvc`. | I |
| 40 | Default harness sizing (cpu `1` / mem `16Gi`) thrashes | It fails at the dataset-prep step. Use cpu `8` / mem `32Gi`. | I |
| 41 | Load has to come from inside the cluster | Driving requests with `curl` through a `kubectl port-forward` cannot saturate real GPUs — the single proxied TCP connection, not the accelerator, becomes the bottleneck. | E |
| 42 | Multi-stack runs render stack 1's model name for every stack | Observed: the qwen3-32b pass rendered `model_name: Qwen/Qwen3-8B` and silently re-ran the 8B benchmark. Run one stack at a time with explicit `--stack <name> -m <model>` and verify the rendered profile. | I |
| 43 | `ttft-aware-scorer` cold start dominates a short run | Whichever endpoint crosses `minRequests` (default 10) first becomes `trusted`; the other reads cold, scores 0, and gets only the `explorationRate` probes. Measured on a 400-request Kind arm: mc-b became trusted at request #107, and the split was 77/23 *the wrong way* before that and 18/82 after — so two runs of the same config gave 24/76 and 40/60. Compare arms on the post-calibration segment, or make the run long enough that warm-up is a small fraction. | E |
| 44 | In-flight counter leak locks traffic onto a drowned backend | `request-metadata-extractor` increments `Requests` per request but decrements only on *response* events. Envoy 504/503s never emit one, so a failure storm leaves the counter stuck high, `idleness` goes to 0, the staleness decay never engages, and the saturated backend's EMA freezes. Restart IPP between A/B arms. | I |
| 45 | A stage that produced 0 requests still exits 0 | Check `total=` in the `DONE` line. | I |
| 46 | The harness can hang in "Collecting results" long after the pods finish | Observed at 33 min. The data survives because the routing counters come from vLLM, not the collector — do not assume the run is lost. | E |
| 47 | Keep `--max-model-len` identical across compared runs | A smaller cap fits more requests in KV and *moves the saturation knee*. | I |
| 48 | Freshly scaled decode pods need ~2 min of EPP scrape lag | Until the pool reports endpoints every request is a 503 and "total served: 0". | I |
| 49 | The TTFT arm records nothing unless the workload streams | `latency-observer-producer` takes TTFT from the first response chunk and **discards single-chunk responses**. A non-streaming profile yields zero observations, every endpoint scores equal, and the arm is indistinguishable from `random-picker`. All profiles here set `api.streaming: true`. | E+I |

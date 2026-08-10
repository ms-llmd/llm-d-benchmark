# ocp-costguard: CostGuard evaluation on OpenShift with real Qwen models

Configuration for evaluating the [CostGuard](https://github.com/kubernetes-sigs/gateway-api-inference-extension/)
scorer against real Qwen models running on H100-80GB GPUs, using the
`cicd/ocp-qwen-gemma-multi` scenario (Qwen3-8B + Qwen3-32B).

This is the OCP counterpart to
[`../kind-costguard/`](../kind-costguard/), which does the same evaluation
against simulator pods on kind.

## Files

- **`costguard-ocp-values.yaml`** — Helm values for `helm upgrade --install
  payload-processor`. Wires the CostGuard scorer + `max-score-picker` in the
  request pipeline and the `model-cost-extractor` + `model-config-datasource`
  in the datalayer. Includes a `payloadProcessor.models` pricing block
  populated with DashScope-anchored rates for Qwen-Plus / Qwen-Max tiers
  (see [Pricing rationale](#pricing-rationale) below).

## Runbook

Prereqs: OCP cluster with ≥2 H100-80GB nodes and the NVIDIA GPU operator,
`oc login` complete, `IPP_PATH` pointing at a
[`llm-d-inference-payload-processor`](https://github.com/llm-d/llm-d-inference-payload-processor)
checkout, `HF_TOKEN` in your environment with license acceptance for
Qwen (both are public non-gated models but the standup expects the secret),
and the IPP image at `$IPP_IMAGE_REPO:$IPP_IMAGE_TAG` pushed to a registry the
cluster can pull from.

```bash
export NAMESPACE=llm-d-<you>
export IPP_PATH=/path/to/llm-d-inference-payload-processor
export IPP_IMAGE_REPO=ghcr.io/<you>/llm-d-inference-payload-processor
export IPP_IMAGE_TAG=costguard

# 1. Build + push the IPP image (from the IPP repo checkout).
(cd "$IPP_PATH" && VERSION="$IPP_IMAGE_TAG" make image-build \
  && docker push "$IPP_IMAGE_REPO:$IPP_IMAGE_TAG")

# 2. Stand up the two-pool Qwen scenario (~60GB + ~64GB weight pull, first time only).
llmdbenchmark --spec cicd/ocp-qwen-gemma-multi standup -p "$NAMESPACE"

# 3. Deploy IPP with the CostGuard OCP values file.
./ipp_benchmarking/tools/ipp_deploy_ocp.sh
```

`ipp_deploy_ocp.sh` will:

- verify the image is pullable and the standup left the expected Gateway +
  InferencePools in place;
- `helm upgrade --install` with the values file in this directory;
- apply the two Qwen BaseModel ConfigMaps;
- render + apply the header-match HTTPRoutes from
  [`../qwen-gemma-httproutes.yaml`](../qwen-gemma-httproutes.yaml)
  (patching the Gateway name and this-standup's InferencePool hashes in
  place);
- verify `costguard` and `model-cost-extractor` show up in the pod's
  loaded-plugin log line;
- remind you to patch `--max-model-len 8192` onto the Qwen3-32B decode
  deployment if the flag isn't already there
  (see [AGENTS.md must-do #2](../../AGENTS.md)).

## Sending traffic

Use the paired workload profile that pins client `max_tokens=1024` so the
pricing-only asymmetry produces a deterministic per-request cost delta:

```bash
llmdbenchmark --spec cicd/ocp-qwen-gemma-multi run \
  -l inference-perf -w ocp-costguard-large-tokens.yaml -p "$NAMESPACE"
NAMESPACE="$NAMESPACE" ./ipp_benchmarking/collect_logs.sh
```

Expected artifacts:

- `per_request_lifecycle_metrics.json` shows every request producing
  ~1024 output tokens (both models honor the client cap; no
  `--max-model-len` truncation).
- The `model` field alternates between `Qwen/Qwen3-8B` and `Qwen/Qwen3-32B`
  based on CostGuard's per-request scoring — under a pricing-only asymmetry
  and no other pressure, CostGuard should favor the cheaper Qwen3-8B most
  of the time.
- The `payload-processor` pod logs show `costguard` scoring decisions with
  ~5× cost deltas between the two model routes.

## Pricing rationale

The pricing block in `costguard-ocp-values.yaml` uses public DashScope API
rates (Alibaba Cloud Model Studio):

| Model | Input $/M | Output $/M | Tier |
|-------|-----------|-----------|------|
| Qwen3-8B  | $0.4 | $1.2 | Qwen-Plus |
| Qwen3-32B | $1.6 | $6.4 | Qwen-Max  |

Ratio: **~4× on input, ~5.3× on output.** At 1024 output tokens per
request (the shape the paired workload profile pins), that's:

- Qwen3-8B  cost = 1024 × 1.2 / 1e6 ≈ **$0.00123** per request
- Qwen3-32B cost = 1024 × 6.4 / 1e6 ≈ **$0.00655** per request

That ~5× cost delta is large enough that CostGuard's `[0, 1]` per-backend
score will differentiate cleanly and pick the cheaper backend whenever
quality constraints don't force a route to the more expensive model.

**When to swap in different pricing.** If you want to evaluate CostGuard
against your organization's actual cost model, replace the four
`inputPerMillion` / `outputPerMillion` fields with $/token numbers derived
from your $/GPU-hour × observed tokens/sec per model. Leave the rest of the
values file alone — the plugin ordering and `datalayer` wiring are what
CostGuard was designed against.

## Why pricing-only asymmetry (option A)

The [kind CostGuard path](../kind-costguard/) creates its cost signal by
patching different `--max-model-len` values into the two sim pods (opt-125m
gets 512, opt-350m gets 64), so a large client `max_tokens` produces
divergent output lengths between the two backends. That approach is
inappropriate on OCP because:

1. Both real Qwen decode pods run with `--max-model-len 8192` (required by
   AGENTS.md must-do #2 to keep Qwen3-32B from crash-looping).
2. Artificially shrinking a real model's context window to force a
   truncation asymmetry would break the model in a way that's unrepresentative
   of production behavior.

So on OCP the cost signal comes entirely from the pricing table × output
tokens. This has the added benefit that the cost model itself is being
evaluated: CostGuard's scoring should react to pricing changes, not to
plumbing quirks of the deployment.

## Also see

- [`../kind-costguard/`](../kind-costguard/) — same evaluation on kind
  with simulator pods (no GPU required).
- [`../../tools/ipp_deploy_ocp.sh`](../../tools/ipp_deploy_ocp.sh) — the
  deploy driver for this configuration.
- [`../../workload/profiles/inference-perf/ocp-costguard-large-tokens.yaml.in`](../../workload/profiles/inference-perf/ocp-costguard-large-tokens.yaml.in) —
  the paired workload profile.
- [`../qwen-gemma-httproutes.yaml`](../qwen-gemma-httproutes.yaml) —
  template for the header-match HTTPRoutes (Gateway name + InferencePool
  hashes patched in place at deploy time).
- [`../../AGENTS.md`](../../AGENTS.md) — required post-standup patches and
  troubleshooting for the OCP path.

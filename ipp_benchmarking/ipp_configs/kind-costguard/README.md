# kind-costguard: generated deploy artifacts for `ipp_deploy.sh`

Files in this directory are **generated on demand** by
[`../../tools/ipp_deploy.sh`](../../tools/ipp_deploy.sh) when it stands up an
IPP with the CostGuard scorer against a kind cluster (as bootstrapped by
[`../../tools/mac_colima_bootstrap.sh`](../../tools/mac_colima_bootstrap.sh)).

They live under `ipp_configs/` so all IPP-evaluation configuration stays inside
the `ipp_benchmarking/` bundle and never leaks into the `IPP_PATH` checkout of
the upstream IPP repo.

## Files

- `costguard-kind-values.yaml` — Helm values passed to `helm upgrade --install
  payload-processor`. Configures the `costguard` scorer alongside
  `max-score-picker`, wires the `model-cost-extractor` extractor under
  `datalayer.extractors`, and enables `model-config-datasource` reading
  `/config/models.json` (see the models.json note below).

## Regenerating vs. editing

`ipp_deploy.sh` generates any missing file with sensible defaults, but **reuses
existing files without modification**. To regenerate a file from defaults,
delete it and re-run `ipp_deploy.sh`. To customize (e.g. tune plugin
parameters, adjust list of models, or use per-token prices that reflect real
provider costs), edit the file in place — subsequent `ipp_deploy.sh` runs will
respect your edits.

## Related, not generated here

The **`opt-125m-base-model.yaml`** and **`opt-350m-base-model.yaml`** labeled
ConfigMaps consumed by the `base-model-to-header` plugin live in the parent
directory (`../opt-125m-base-model.yaml`, `../opt-350m-base-model.yaml`) and
are hand-authored, not tool-generated.

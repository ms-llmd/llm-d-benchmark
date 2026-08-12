#!/usr/bin/env python3
"""Did the TTFT-aware scorer predict well? Actual vs predicted, per request.

    plot_ttft_accuracy.py <epp.log> [<epp.log> ...] --labels 8b,32b [-o out.png]

Both halves of the pair are in the router EPP's own --v=4 log, one line each:

  ttft-aware score   x-request-id, endpoint, predictedTTFT   (before dispatch)
  ttft-observation   x-request-id, endpoint, ttftSeconds,
                     inflightAtDispatch                      (at first chunk)

so a request is joined on (id, endpoint) and needs nothing from the harness.
Only the endpoint that actually served the request has an observation, so the
losing endpoints' predictions drop out on the join.

Left panel is the parity plot. Right is the thing the model is actually claiming:
TTFT rises with the in-flight count an endpoint carries at dispatch, so both
series are binned against that and overlaid -- a predictor can sit close on the
parity plot and still have the wrong slope here, which is what decides routing.

Predictions made while the endpoint was still uncalibrated (`trusted:false`) are
seeds, not predictions, and are reported separately rather than scored.
"""
import argparse, json, os, sys, tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]


def load_pairs(path):
    """[(predicted, actual, inflight, trusted)] joined on (request id, endpoint)."""
    pred, obs = {}, {}
    for line in open(path, errors="replace"):
        if "ttft-aware score" not in line and "ttft-observation" not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = d.get("x-request-id")
        if not rid:
            continue
        if d.get("msg") == "ttft-aware score":
            # endpoint is the struct's String(), e.g. "{ID:default/mc-a Name:mc-a ...}"
            ep = d.get("endpoint", "")
            ep = ep.split("ID:", 1)[1].split(" ", 1)[0] if "ID:" in ep else ep
            pred[(rid, ep)] = (d.get("predictedTTFT"), bool(d.get("trusted")))
        elif d.get("msg") == "ttft-observation":
            obs[(rid, d.get("endpoint"))] = (d.get("ttftSeconds"), d.get("inflightAtDispatch"))

    out = []
    for key, (actual, inflight) in obs.items():
        if key not in pred:
            continue
        p, trusted = pred[key]
        if p is None or actual is None:
            continue
        out.append((float(p), float(actual), float(inflight or 0), trusted))
    if not out:
        sys.exit(f"{path}: no joinable records -- was the EPP run with --v=4 on the ttft arm?")
    return np.array(out, dtype=float)


def stats(p, a):
    err = p - a
    return {
        "n": len(a),
        "MAE": np.mean(np.abs(err)),
        "median AE": np.median(np.abs(err)),
        "bias": np.mean(err),
        "MAPE%": 100 * np.mean(np.abs(err) / np.maximum(a, 1e-9)),
        "r": np.corrcoef(p, a)[0, 1] if len(a) > 1 and p.std() > 0 else float("nan"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="+")
    ap.add_argument("--labels", default=None, help="comma-separated, one per log")
    ap.add_argument("--bin", type=float, default=2.0, help="in-flight bin width")
    ap.add_argument("--log", action="store_true", help="log-log parity axes")
    ap.add_argument("--title", default="TTFT-aware scorer: predicted vs actual")
    ap.add_argument("-o", "--output", default="ttft_accuracy.png")
    a = ap.parse_args()

    labels = a.labels.split(",") if a.labels else [os.path.basename(x) for x in a.logs]
    if len(labels) != len(a.logs):
        sys.exit("--labels count must match the number of logs")

    fig, (ax, ax2) = plt.subplots(1, 2, figsize=(14, 6))
    hi = 0

    for i, (path, label) in enumerate(zip(a.logs, labels)):
        rows = load_pairs(path)
        cold = rows[~rows[:, 3].astype(bool)]
        warm = rows[rows[:, 3].astype(bool)]
        c = COLORS[i % len(COLORS)]

        s = stats(warm[:, 0], warm[:, 1]) if len(warm) else None
        print(f"\n=== {label} ===")
        print(f"  joined {len(rows)} requests ({len(cold)} uncalibrated, reported not scored)")
        if s:
            print(f"  n={s['n']}  MAE={s['MAE']:.3f}s  median AE={s['median AE']:.3f}s  "
                  f"bias={s['bias']:+.3f}s  MAPE={s['MAPE%']:.1f}%  r={s['r']:.3f}")
        if len(cold):
            print(f"  uncalibrated: MAE={np.mean(np.abs(cold[:, 0] - cold[:, 1])):.3f}s")

        if len(warm):
            ax.scatter(warm[:, 0], warm[:, 1], s=8, alpha=0.25, color=c, linewidths=0,
                       label=f"{label} (n={s['n']}, MAE {s['MAE']:.2f}s, r {s['r']:.2f})")
            hi = max(hi, warm[:, 0].max(), warm[:, 1].max())
        if len(cold):
            ax.scatter(cold[:, 0], cold[:, 1], s=8, alpha=0.18, color=c, linewidths=0,
                       marker="x", label=f"{label} uncalibrated (n={len(cold)})")

        # Right panel: both series against the load the curve is a function of.
        src = warm if len(warm) else rows
        b = (src[:, 2] / a.bin).astype(int)
        ks = np.unique(b)
        xs = (ks + 0.5) * a.bin
        ax2.plot(xs, [np.median(src[b == k, 1]) for k in ks], "o-", color=c, lw=2,
                 label=f"{label} actual")
        ax2.plot(xs, [np.median(src[b == k, 0]) for k in ks], "s--", color=c, lw=1.6,
                 alpha=0.75, label=f"{label} predicted")

    lim = hi * 1.05 or 1
    ax.plot([0, lim], [0, lim], "-", color="0.4", lw=1, zorder=0)
    ax.text(lim * 0.97, lim * 0.9, "perfect", color="0.4", fontsize=8, ha="right")
    if a.log:
        ax.set_xscale("log"); ax.set_yscale("log")
    else:
        ax.set_xlim(0, lim); ax.set_ylim(0, lim)
    ax.set_xlabel("predicted TTFT (s)")
    ax.set_ylabel("actual TTFT (s)")
    ax.set_title("parity")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper left", framealpha=0.95)

    ax2.set_xlabel("in-flight requests on that endpoint at dispatch")
    ax2.set_ylabel("TTFT (s), median per bin")
    ax2.set_title("does the curve track load?")
    ax2.grid(alpha=0.3)
    ax2.legend(fontsize=8, loc="upper left", framealpha=0.95)

    fig.suptitle(a.title)
    fig.tight_layout()
    fig.savefig(a.output, dpi=130)
    print("\nwrote", a.output)


if __name__ == "__main__":
    main()

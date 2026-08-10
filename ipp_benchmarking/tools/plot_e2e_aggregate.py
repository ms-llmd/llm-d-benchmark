#!/usr/bin/env python3
"""Aggregated (whole-run) e2e/ttft latency percentiles, one or more runs grouped.
From inference-perf summary_lifecycle_metrics.json.

  plot_e2e_aggregate.py <label>=<dir> [<label>=<dir> ...] [-o out.png] [--metric e2e]
"""
import argparse, json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

METRIC = {"ttft": "time_to_first_token", "e2e": "request_latency"}
PCTS = [("median", "p50"), ("p90", "p90"), ("p95", "p95"), ("p99", "p99")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="label=dir (with summary_lifecycle_metrics.json)")
    ap.add_argument("-o", "--output", default="e2e_aggregate.png")
    ap.add_argument("--metric", choices=["ttft", "e2e"], default="e2e")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()
    lbl = {"ttft": "time to first token", "e2e": "end-to-end latency"}[a.metric]

    data = {}
    for r in a.runs:
        lab, d = r.split("=", 1)
        t = json.load(open(f"{d}/summary_lifecycle_metrics.json"))["successes"]["latency"][METRIC[a.metric]]
        data[lab] = [t[k] for k, _ in PCTS]

    colors = ["#9467bd", "#2ca02c", "#1f77b4", "#d62728"]
    x = np.arange(len(PCTS)); w = 0.8 / len(data)
    fig, ax = plt.subplots(figsize=(8, 5))
    for i, (lab, ys) in enumerate(data.items()):
        b = ax.bar(x + i * w, ys, w, color=colors[i % len(colors)], label=lab)
        ax.bar_label(b, fmt="%.1f", fontsize=8, padding=2)
    ax.set_xticks(x + w * (len(data) - 1) / 2); ax.set_xticklabels([p for _, p in PCTS])
    ax.set_ylabel(f"{lbl} (s)")
    ax.set_title(a.title or f"aggregated {lbl} (whole run)")
    ax.grid(alpha=.3, axis="y")
    ax.legend(fontsize=9, loc="upper left", bbox_to_anchor=(1.0, 1.0))
    fig.tight_layout(); fig.savefig(a.output, dpi=130, bbox_inches="tight")
    print("wrote", a.output)
    for lab, ys in data.items():
        print(lab, "  ".join(f"{p}={y:.2f}s" for (_, p), y in zip(PCTS, ys)))


if __name__ == "__main__":
    main()

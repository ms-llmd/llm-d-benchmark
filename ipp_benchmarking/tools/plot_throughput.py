#!/usr/bin/env python3
"""Achieved request throughput (completions/s), per-stage or over-time, runs overlaid.

  plot_throughput.py <label>=<dir> [<label>=<dir> ...] --mode perstage|timeseries \
      [-o out.png] [--bin 15] [--title T]

perstage:   inference-perf successes.throughput.requests_per_sec per stage vs the requested-RPS
            ladder, with the offered rate as a dashed reference (gap = saturation shortfall).
timeseries: completions per REAL wall-clock bin / bin_s (true req/s), each run warped to its own
            stage windows so identical stages line up despite pacing drift.
"""
import argparse, glob, json
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from plot_e2e_timeseries import stage_elapsed, to_stage_x, rps_ladder

COLORS = ["#9467bd", "#2ca02c", "#1f77b4", "#d62728", "#ff7f0e"]
STAGE_KEY = {"requests": "requests_per_sec", "output_tokens": "output_tokens_per_sec",
             "total_tokens": "total_tokens_per_sec"}
YLAB = {"requests": "throughput (completions/s)", "output_tokens": "output tokens/s (goodput)",
        "total_tokens": "total tokens/s"}


def stage_thruput(d, metric):
    out = []
    for f in sorted(glob.glob(f"{d}/stage_*_lifecycle_metrics.json"),
                    key=lambda p: int(p.split("stage_")[1].split("_")[0])):
        x = json.load(open(f))
        out.append((x["load_summary"]["requested_rate"],
                    x["successes"]["throughput"][STAGE_KEY[metric]]))
    return out


def thruput_series(d, bin_s, metric, align=False):
    recs = json.load(open(f"{d}/per_request_slim.json"))
    ok = [r for r in recs if not r["fail"]]
    ct0 = np.array([r["t"] + r["lat"] for r in ok])
    o = np.argsort(ct0); ct = ct0[o] - min(r["t"] for r in ok)  # completion times, elapsed
    wt = None if metric == "requests" else np.array([r.get("ot") or 0 for r in ok])[o]
    W = stage_elapsed(f"{d}/harness_stdout.log")
    def binned(edges):
        y, e = np.histogram(ct, bins=edges, weights=wt)
        return to_stage_x(e[:-1] + np.diff(e) / 2, W), y / np.diff(e)   # per-second rate
    if not align:
        return binned(np.arange(0, ct.max() + bin_s, bin_s))
    xs, ys = [], []                                 # bin within each stage window; break line across gaps
    for _, S, E in W:
        x, y = binned(np.linspace(S, E, max(1, round((E - S) / bin_s)) + 1))
        xs += list(x) + [np.nan]; ys += list(y) + [np.nan]
    return np.array(xs), np.array(ys)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="label=dir")
    ap.add_argument("--mode", choices=["perstage", "timeseries"], required=True)
    ap.add_argument("--metric", choices=list(STAGE_KEY), default="requests")
    ap.add_argument("--bin", type=float, default=15.0)
    ap.add_argument("--align-stages", action="store_true", help="anchor bins to stage windows (no boundary straddle)")
    ap.add_argument("-o", "--output", default="throughput.png")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()
    if a.mode == "timeseries" and a.metric == "total_tokens":
        raise SystemExit("timeseries has no input-token data; use --metric output_tokens or requests")
    runs = [r.split("=", 1) for r in a.runs]

    fig, ax = plt.subplots(figsize=(13, 6) if a.mode == "timeseries" else (10, 5.5))
    if a.mode == "perstage":
        ref = stage_thruput(runs[0][1], a.metric)
        N = len(ref)
        if a.metric == "requests":
            ax.plot(range(N), [r[0] for r in ref], "--", color="0.5", lw=1.2, label="offered (requested RPS)")
        for i, (lab, d) in enumerate(runs):
            s = stage_thruput(d, a.metric)
            ax.plot(range(len(s)), [v for _, v in s], "-o", color=COLORS[i % len(COLORS)], label=lab)
        ax.set_xticks(range(N)); ax.set_xticklabels([f"{r:g}" for r, _ in ref])
        ax.set_xlabel("stage requested RPS")
    else:
        RPS = rps_ladder(runs[0][1]); N = len(RPS)
        for n in range(N):
            ax.axvspan(n, n + 1, color="0.90" if n % 2 == 0 else "0.83", zorder=0)
            ax.text(n + 0.5, 0.96, f"{RPS[n]:g}", transform=ax.get_xaxis_transform(),
                    ha="center", va="top", fontsize=8, color="dimgray")
        for i, (lab, d) in enumerate(runs):
            x, y = thruput_series(d, a.bin, a.metric, a.align_stages)
            ax.plot(x, y, "-", color=COLORS[i % len(COLORS)], lw=1.6, label=lab)
        ax.set_xlim(-0.3, N + 0.3)
        ax.set_xticks([n + 0.5 for n in range(N)])
        ax.set_xticklabels([f"{RPS[n]:g}" for n in range(N)])
        ax.set_xlabel(f"stage requested RPS ({a.bin:g}s bins)")

    ax.set_ylabel(YLAB[a.metric])
    ax.set_title(a.title or f"{a.metric} throughput ({a.mode})")
    ax.grid(alpha=.3)
    ax.legend(fontsize=9, loc="upper left", bbox_to_anchor=(1.0, 1.0), framealpha=0.95)
    fig.tight_layout(); fig.savefig(a.output, dpi=130, bbox_inches="tight")
    print("wrote", a.output)
    if a.mode == "perstage":
        for lab, d in runs:
            print(lab, "peak =", round(max(v for _, v in stage_thruput(d, a.metric)), 1), YLAB[a.metric])


if __name__ == "__main__":
    main()

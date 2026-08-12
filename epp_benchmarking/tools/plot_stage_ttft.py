#!/usr/bin/env python3
"""p50 TTFT per ladder stage, one figure per arm plus an aggregate.

    plot_stage_ttft.py <arm-dir> [<arm-dir> ...] --labels queue,ttft,random -o <outdir>

Each <arm-dir> holds that arm's harness results (the `stage_*_lifecycle_metrics.json`
files, at any depth). The median is read straight from the harness, not
recomputed, so this works for every arm including `random-picker` — whose EPP
log carries no TTFT observations at all, because without a ttft-aware-scorer
there is no latency-observer-producer to write them.

x is stage in run order, not offered rate: the ladder climbs and comes back
down, so plotting against rate would fold the descent onto the ascent and hide
any hysteresis — which is the interesting part, since a leaf that fell behind on
the way up is still draining on the way down.

The band is p25-p75. p50 alone says where the middle sat; the band says whether
the arm was merely slower or had come apart.
"""
import argparse, glob, json, os, sys, tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]


def load_arm(d, stat, lo, hi):
    """[(stage, rate, lo, stat, hi)] in stage order, for the requested keys."""
    files = sorted(glob.glob(f"{d}/**/stage_*_lifecycle_metrics.json", recursive=True))
    if not files:
        sys.exit(f"{d}: no stage_*_lifecycle_metrics.json found")
    rows = {}
    for f in files:
        n = int(os.path.basename(f).split("stage_")[1].split("_")[0])
        j = json.load(open(f))
        t = j["successes"]["latency"]["time_to_first_token"]
        for k in (stat, lo, hi):
            if k not in t:
                sys.exit(f"{f}: no '{k}' in time_to_first_token (have {sorted(t)})")
        s = j["load_summary"]
        rate = s.get("requested_rate") if not s.get("concurrency") else s["concurrency"]
        rows[n] = (n, rate, t[lo], t[stat], t[hi])
    return [rows[n] for n in sorted(rows)]


def draw(ax, arms, labels, stat, lo, hi):
    for i, (rows, label) in enumerate(zip(arms, labels)):
        a = np.array(rows, dtype=float)
        c = COLORS[i % len(COLORS)]
        ax.fill_between(a[:, 0], a[:, 2], a[:, 4], color=c, alpha=0.15, linewidth=0)
        ax.plot(a[:, 0], a[:, 3], "o-", color=c, lw=2.2,
                label=f"{label} — {stat} (band {lo}-{hi})")
    stages = arms[0]
    ax.set_xticks([r[0] for r in stages])
    ax.set_xticklabels([f"{r[0]}\n{r[1]:g} rps" for r in stages], fontsize=9)
    ax.set_xlabel("ladder stage (in run order)")
    ax.set_ylabel("TTFT (s)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9, loc="upper left", framealpha=0.95)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--labels", default=None)
    ap.add_argument("--stat", default="median",
                    help="percentile key to plot: median, p75, p90, p95, p99, mean")
    ap.add_argument("--band", default="p25,p75", help="lo,hi keys for the shaded band")
    ap.add_argument("--title", default=None)
    ap.add_argument("-o", "--outdir", required=True)
    a = ap.parse_args()

    labels = a.labels.split(",") if a.labels else [os.path.basename(x.rstrip("/")) for x in a.arms]
    if len(labels) != len(a.arms):
        sys.exit("--labels count must match the number of arm directories")
    lo, hi = a.band.split(",")
    name = "p50" if a.stat == "median" else a.stat
    title = a.title or f"{name} TTFT per ladder stage"
    os.makedirs(a.outdir, exist_ok=True)

    arms = [load_arm(d, a.stat, lo, hi) for d in a.arms]

    for rows, label in zip(arms, labels):
        print(f"\n=== {label} ({name}) ===")
        print(f"{'stage':>5} {'rate':>7} {lo:>8} {name:>8} {hi:>8}")
        for n, rate, v_lo, v, v_hi in rows:
            print(f"{n:>5} {rate:>7.0f} {v_lo:>8.2f} {v:>8.2f} {v_hi:>8.2f}")
        fig, ax = plt.subplots(figsize=(11, 5))
        draw(ax, [rows], [label], name, lo, hi)
        ax.set_title(f"{title} — {label}")
        fig.tight_layout()
        out = os.path.join(a.outdir, f"{name}_ttft_{label}.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print("wrote", out)

    fig, ax = plt.subplots(figsize=(12, 6))
    draw(ax, arms, labels, name, lo, hi)
    ax.set_title(f"{title} — all arms")
    fig.tight_layout()
    out = os.path.join(a.outdir, f"{name}_ttft_aggregate.png")
    fig.savefig(out, dpi=130)
    print("\nwrote", out)


if __name__ == "__main__":
    main()

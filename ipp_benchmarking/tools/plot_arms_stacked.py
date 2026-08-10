#!/usr/bin/env python3
"""Both arms on one canvas: smart routing on top, random on the bottom, same stage in the
same column.

    plot_arms_stacked.py <smart dir> <random dir> -o out.png [--metric ttft|e2e] [--pct 95]

Each stage gets a slot as wide as the LONGER of the two arms' runs of it, so a stage's column
covers the same x range in both rows even when one arm drains slower. Data loading is reused
from plot_adaptive_stages (read_stage/lifecycle) so both figures show the same numbers.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# the 20 rps shared-only stage is s0_shared20 in the 5-stage arm, s6_shared20 in the 7-stage one
PAIRS = [("s-1_baseline1", "s-1_baseline1"), ("s1_pinGemma", "s1_pinGemma"),
         ("s3_pinQwen", "s3_pinQwen"), ("s4_both", "s4_both"), ("s0_shared20", "s6_shared20")]
GAP, BIN = 20.0, 30.0
STREAM_C = {"shared": "#1f77b4", "gemma": None, "qwen": None}   # filled from plot_adaptive_stages


def load_mod():
    spec = importlib.util.spec_from_file_location(
        "pas", Path(__file__).with_name("plot_adaptive_stages.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def samples(pas, stage_dir: Path, metric: str) -> dict[str, np.ndarray]:
    """-> {pool: [(arrival ts, value)]}"""
    if metric == "e2e":
        rows = [l.split(",") for l in (stage_dir / "e2e.csv").read_text().splitlines()[1:] if l]
        return {p: np.array([(float(r[2]), float(r[4])) for r in rows if r[1] == p])
                for p in ("gemma", "qwen")}
    st = pas.read_stage(str(stage_dir / "ipp-decisions.log"))
    return {p: np.array([(st["ts"][r], tt) for r, (tt, _, pl) in st["actual"].items()
                         if pl == p and r in st["ts"]])
            for p in ("gemma", "qwen")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("smart")
    ap.add_argument("random")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--metric", default="ttft", choices=("ttft", "e2e"))
    ap.add_argument("--pct", type=int, default=95)
    a = ap.parse_args()

    pas = load_mod()
    colors = (("gemma", pas.GEMMA_C), ("qwen", pas.QWEN_C))
    STREAM_C.update(gemma=pas.GEMMA_C, qwen=pas.QWEN_C)
    arms = [("smart (ttft-aware scorer)", Path(a.smart), 0), ("random picker", Path(a.random), 1)]
    data = {i: {} for _, _, i in arms}
    offered: dict[int, list] = {}      # si -> [(stream, requested rps)], both arms identical
    for _, root, i in arms:
        for si, (s_smart, s_rand) in enumerate(PAIRS):
            d = root / (s_smart if i == 0 else s_rand)
            if (d / "e2e.csv").is_file():
                data[i][si] = samples(pas, d, a.metric)
                lf = pas.lifecycle(str(d))
                rps = [(s, lf[s]["rate"]) for s in ("gemma", "qwen", "shared") if s in lf]
                if offered.setdefault(si, rps) != rps:
                    print(f"warn: {s_smart}/{s_rand} offered load differs between arms: "
                          f"{offered[si]} vs {rps}", file=sys.stderr)

    logy = a.metric == "ttft"
    minbin = 8 if a.pct <= 50 else 30
    fig, axes = plt.subplots(2, 1, figsize=(16, 9.5), sharex=True, sharey=True)
    x, peak, slots = 0.0, 0.0, []
    for si, (s_smart, s_rand) in enumerate(PAIRS):
        durs = [v[:, 0].max() - v[:, 0].min()
                for i in data for v in data[i].get(si, {}).values() if len(v)]
        if not durs:
            continue
        width = max(durs)
        slots.append((si, x, width, s_smart if s_smart == s_rand else f"{s_smart} / {s_rand}"))
        for i, ax in enumerate(axes):
            by_pool = data[i].get(si, {})
            t0 = min([v[:, 0].min() for v in by_pool.values() if len(v)] or [0])
            for pool, c in colors:
                v = by_pool.get(pool, np.empty((0, 2)))
                if len(v) < minbin:
                    continue
                rel, val = v[:, 0] - t0, v[:, 1]
                ax.scatter(x + rel, val, s=2.5, alpha=0.12, color=c, edgecolors="none")
                bins = np.arange(0, rel.max() + BIN, BIN)
                idx = np.digitize(rel, bins)
                y = [np.percentile(val[idx == k], a.pct) if (idx == k).sum() >= minbin else np.nan
                     for k in range(1, len(bins) + 1)]
                ax.plot(x + bins, y, color=c, lw=2.4)
                peak = max(peak, float(np.nanmax(y)))
                m = float(np.percentile(val, a.pct))
                ax.annotate(f"p{a.pct} {m:.3f}s" if m < 1 else f"p{a.pct} {m:.1f}s",
                            (x + 6, m), fontsize=8, color=c, ha="left",
                            textcoords="offset points", xytext=(0, 9 if pool == "gemma" else -14),
                            bbox=dict(fc="white", ec="none", alpha=0.75, pad=0.15))
            ax.axvspan(x, x + width, color="black", alpha=0.03)
        x += width + GAP

    lab = "time to first token (s)" if a.metric == "ttft" else "end-to-end latency (s)"
    for (name, _, i), ax in zip(arms, axes):
        for pool, c in colors:
            ax.plot([], [], color=c, lw=2.4, label=f"{pool}: p{a.pct} of every request routed there")
        # inside the axes: an axes title on the top row would collide with the stage labels
        ax.text(0.995, 0.955, name, transform=ax.transAxes, ha="right", va="top",
                fontsize=12, fontweight="bold",
                bbox=dict(fc="white", ec="0.7", alpha=0.9, pad=0.4))
        ax.set_ylabel(lab, fontsize=9)
        ax.grid(alpha=0.3, axis="y")
        ax.legend(fontsize=9, loc="upper left", ncol=2)
        if logy:
            ax.set_yscale("log")
            ax.set_ylim(0.02, peak * 3)
            ax.set_yticks([0.05, 0.25, 1, 5, 25, 100])
            ax.set_yticklabels(["50 ms", "250 ms", "1 s", "5 s", "25 s", "100 s"], fontsize=8)
        else:
            ax.set_ylim(0, peak * 1.35)
    for si, x0, w, s in slots:      # offered load + stage name above the top row (both arms alike)
        rps = offered.get(si, [])
        for i, (stream, r) in enumerate(rps):     # stack upward, never into the plot
            axes[0].text(x0 + w / 2, 1.02 + 0.052 * (len(rps) - 1 - i), f"{stream} {r:.0f} rps",
                         transform=axes[0].get_xaxis_transform(), ha="center", va="bottom",
                         fontsize=8.5, color=STREAM_C[stream])
        axes[0].text(x0 + w / 2, 1.04 + 0.052 * len(rps), s,
                     transform=axes[0].get_xaxis_transform(),
                     ha="center", va="bottom", fontsize=9, fontweight="bold")
    axes[0].set_xlim(-GAP / 2, x - GAP / 2)
    axes[1].set_xlabel("elapsed time across the experiment (s), stages laid end to end; "
                       "each stage's slot is as wide as the slower arm", fontsize=9)
    fig.suptitle(f"Same stages, both routers: per-request {lab.split(' (')[0]} by serving model "
                 f"- line is the p{a.pct} per {BIN:.0f} s", fontsize=12)
    fig.tight_layout(rect=(0, 0, 1, 0.93))     # headroom for the per-stage offered-load labels
    fig.savefig(a.out, dpi=140)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

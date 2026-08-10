#!/usr/bin/env python3
"""p50 TTFT across the whole stage timeline, two views.

    plot_ttft_timeline.py <run dir> -o <out-stem>

  <out-stem>_by_model.png     one line per SERVING MODEL -- every request that landed on that pool,
                              pinned and shared pooled together (what the GPU experienced)
  <out-stem>_by_workload.png  one line per LOAD STREAM -- shared ("auto") separated from each
                              pinned stream (what each client experienced)

Per-request TTFT comes from the IPP "ttft-observation" line, joined to e2e.csv on x-request-id for
the arrival timestamp and the shared/pinned flag. Stages are laid end to end with a small gap.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

STAGES = ["s-1_baseline1", "s0_baseline2", "s1_pinGemma", "s2_release",
          "s3_pinQwen", "s4_both", "s5_release"]
GEM, QW, SH = "#7b3fa0", "#2e8b57", "#1f77b4"
# streams active per stage, drawn above the band in each stream's own colour
CFG = {"s-1_baseline1": [("gemma 10", GEM), ("qwen 10", QW)],
       "s0_baseline2":  [("shared 10", SH)],
       "s1_pinGemma":   [("shared 10", SH), ("gemma 10", GEM)],
       "s2_release":    [("shared 10", SH)],
       "s3_pinQwen":    [("shared 10", SH), ("qwen 10", QW)],
       "s4_both":       [("shared 10", SH), ("gemma 10", GEM), ("qwen 10", QW)],
       "s5_release":    [("shared 10", SH)]}
BIN, GAP, MINBIN = 30.0, 20.0, 8


def read(root: str, stage: str):
    p = os.path.join(root, stage)
    rows = list(csv.DictReader(open(os.path.join(p, "e2e.csv"))))
    ttft = {}
    for line in open(os.path.join(p, "ipp-decisions.log"), errors="ignore"):
        if '"msg":"ttft-observation"' not in line:
            continue
        rid = re.search(r'"x-request-id":"([^"]+)"', line)
        tt = re.search(r'"ttft_s":([0-9.eE+-]+)', line)
        if rid and tt:
            ttft[rid.group(1)] = float(tt.group(1))
    out = []
    for r in rows:
        if r["rid"] in ttft:
            out.append((float(r["t0"]), ttft[r["rid"]], float(r["e2e"]), r["model"], r["shared"] == "1"))
    return out


def series(recs, mode, metric):
    """-> {label: np.array[(t, value)]}"""
    d = {}
    for t, tt, e2e, model, shared in recs:
        key = model if mode == "model" else ("shared" if shared else f"{model}-pinned")
        d.setdefault(key, []).append((t, tt if metric == "ttft" else e2e))
    return {k: np.array(sorted(v)) for k, v in d.items()}


def draw(root, stages, mode, style, title, path, logy=True, metric="ttft"):
    fig, ax = plt.subplots(figsize=(15, 5.8))
    x, peak = 0.0, 0.0
    for s in stages:
        recs = read(root, s)
        allt = np.array([r[0] for r in recs])
        t0, dur = allt.min(), allt.max() - allt.min()
        for key, (c, lbl) in style.items():
            v = series(recs, mode, metric).get(key)
            if v is None or len(v) < MINBIN:
                continue
            rel, y = v[:, 0] - t0, v[:, 1]
            bins = np.arange(0, rel.max() + BIN, BIN)
            idx = np.digitize(rel, bins)
            p50 = [np.median(y[idx == k]) if (idx == k).sum() >= MINBIN else np.nan
                   for k in range(1, len(bins) + 1)]
            ax.scatter(x + rel, y, s=3, alpha=0.18, color=c, edgecolors="none", zorder=1)
            ax.plot(x + bins, p50, color=c, lw=2.4, zorder=3)
            peak = max(peak, float(np.nanmax(p50)))
        ax.axvspan(x, x + dur, color="black", alpha=0.03)
        rows = CFG.get(s, [])
        for i, (txt, col) in enumerate(rows):      # stack upward so nothing enters the plot
            ax.text(x + dur / 2, 1.02 + 0.05 * (len(rows) - 1 - i), txt, color=col,
                    transform=ax.get_xaxis_transform(), ha="center", va="bottom", fontsize=9)
        ax.text(x + dur / 2, 1.05 + 0.05 * len(rows), s, transform=ax.get_xaxis_transform(),
                ha="center", va="bottom", fontsize=9.5, fontweight="bold")
        x += dur + GAP
    for key, (c, lbl) in style.items():
        ax.plot([], [], color=c, lw=2.4, label=lbl)
    ax.set_xlim(-GAP / 2, x - GAP / 2)
    if logy:
        ax.set_yscale("log")
        # fixed ranges so the smart run and the random baseline are directly comparable, and wide
        # enough that no p50 is clipped (random reaches 53 s TTFT / 116 s e2e under bad routing)
        if metric == "ttft":
            ax.set_ylim(0.05, 250)
            ax.set_yticks([0.1, 0.5, 1, 5, 15, 60, 120])
            ax.set_yticklabels(["100 ms", "500 ms", "1 s", "5 s", "15 s", "60 s", "120 s"], fontsize=9)
        else:
            ax.set_ylim(0.8, 300)
            ax.set_yticks([1, 2, 5, 10, 20, 60, 120])
            ax.set_yticklabels(["1 s", "2 s", "5 s", "10 s", "20 s", "60 s", "120 s"], fontsize=9)
    else:
        ax.set_ylim(0, peak * 1.15)   # linear: sub-second stages collapse onto the axis
    ax.set_xlabel("elapsed time across the experiment (s), stages laid end to end", fontsize=9.5)
    ax.set_ylabel("median (p50) " + ("time to first token" if metric == "ttft" else
                                     "end-to-end latency"), fontsize=9.5)
    ax.grid(alpha=0.3, axis="y")
    ax.legend(fontsize=10, loc="upper left", ncol=3, framealpha=0.95)
    fig.suptitle(title, fontsize=12.5)
    fig.tight_layout()
    fig.savefig(path, dpi=145)
    plt.close(fig)
    print("wrote", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("-o", "--out", default="ttft")
    ap.add_argument("--metric", choices=("ttft", "e2e"), default="ttft")
    ap.add_argument("--linear", action="store_true", help="also emit a linear-axis version")
    args = ap.parse_args()
    stages = [s for s in STAGES if os.path.isfile(os.path.join(args.root, s, "e2e.csv"))]
    name = "TTFT" if args.metric == "ttft" else "end-to-end latency"
    views = (
        ("model", {"gemma": (GEM, "Gemma-4 26B pool"), "qwen": (QW, "Qwen3.6 35B pool")},
         f"p50 {name} per SERVING MODEL \u2014 every request that landed on the pool (pinned + shared)"),
        ("workload", {"shared": (SH, 'shared stream (model:"auto", routed by the scorer)'),
                      "gemma-pinned": (GEM, "Gemma-pinned stream"),
                      "qwen-pinned": (QW, "Qwen-pinned stream")},
         f"p50 {name} per LOAD STREAM \u2014 what each client saw; the shared stream is the routed one"),
    )
    sub = "\ndots are individual requests, line is the p50 per %.0f s" % BIN
    for mode, style, title in views:
        draw(args.root, stages, mode, style, title + sub,
             f"{args.out}_by_{mode}.png", logy=True, metric=args.metric)
        if args.linear:
            draw(args.root, stages, mode, style, title + sub + " \u2014 LINEAR axis",
                 f"{args.out}_by_{mode}_linear.png", logy=False, metric=args.metric)
    return 0


if __name__ == "__main__":
    sys.exit(main())

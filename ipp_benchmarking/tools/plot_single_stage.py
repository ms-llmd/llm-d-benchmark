#!/usr/bin/env python3
"""Two presentation figures for ONE stage: TTFT and e2e latency, per serving model.

    plot_single_stage.py <stage dir> -o <out-stem> [--title "..."]

Per-request, both metrics, joined on x-request-id so each request contributes one point to each:
  e2e.csv            t0 (arrival), e2e, model
  ipp-decisions.log  "ttft-observation" -> ttft_s

Emits <out-stem>_ttft.png and <out-stem>_e2e.png.
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

C = {"gemma": "#7b3fa0", "qwen": "#2e8b57"}
LABEL = {"gemma": "Gemma-4 26B", "qwen": "Qwen3.6 35B"}
BIN = 10.0


def load(stage: str):
    rows = list(csv.DictReader(open(os.path.join(stage, "e2e.csv"))))
    t0 = {r["rid"]: float(r["t0"]) for r in rows}
    pool = {r["rid"]: r["model"] for r in rows}
    e2e = {r["rid"]: float(r["e2e"]) for r in rows}
    ttft = {}
    for line in open(os.path.join(stage, "ipp-decisions.log"), errors="ignore"):
        if '"msg":"ttft-observation"' not in line:
            continue
        rid = re.search(r'"x-request-id":"([^"]+)"', line)
        tt = re.search(r'"ttft_s":([0-9.eE+-]+)', line)
        if rid and tt:
            ttft[rid.group(1)] = float(tt.group(1))
    start = min(t0.values())
    out = {}
    for metric, src in (("e2e", e2e), ("ttft", ttft)):
        d = {}
        for rid, v in src.items():
            if rid in t0:
                d.setdefault(pool[rid], []).append((t0[rid] - start, v))
        out[metric] = {p: np.array(sorted(v)) for p, v in d.items()}
    return out


def figure(data, ylabel, title, path, unit="s"):
    fig, ax = plt.subplots(figsize=(11, 5.4))
    hi = 0.0
    for pool in ("gemma", "qwen"):
        v = data.get(pool)
        if v is None or len(v) < 5:
            continue
        t, y = v[:, 0], v[:, 1]
        ax.scatter(t, y, s=4, alpha=0.13, color=C[pool], edgecolors="none")
        bins = np.arange(0, t.max() + BIN, BIN)
        idx = np.digitize(t, bins)
        p50 = [np.median(y[idx == k]) if (idx == k).any() else np.nan for k in range(1, len(bins) + 1)]
        p50m, p95, p99 = np.median(y), np.percentile(y, 95), np.percentile(y, 99)
        ax.plot(bins, p50, color=C[pool], lw=2.6,
                label=f"{LABEL[pool]}  —  p50 {p50m:.2f}{unit}   p95 {p95:.2f}{unit}   p99 {p99:.2f}{unit}   (n={len(y)})")
        hi = max(hi, float(np.percentile(y, 99)))
    ax.set_xlabel("seconds into stage", fontsize=10)
    ax.set_ylabel(ylabel, fontsize=10)
    ax.set_ylim(0, hi * 1.25)
    ax.set_title(title, fontsize=12)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9.5, loc="upper right", framealpha=0.95)
    fig.tight_layout()
    fig.savefig(path, dpi=145)
    plt.close(fig)
    print("wrote", path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage")
    ap.add_argument("-o", "--out", default="stage")
    ap.add_argument("--title", default="")
    args = ap.parse_args()
    d = load(args.stage)
    suffix = f"\n{args.title}" if args.title else ""
    figure(d["ttft"], "time to first token (s)",
           "Time to first token, per request" + suffix, f"{args.out}_ttft.png")
    figure(d["e2e"], "end-to-end request latency (s)",
           "End-to-end latency, per request" + suffix, f"{args.out}_e2e.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())

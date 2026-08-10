#!/usr/bin/env python3
"""Routing decisions over time, stages laid end to end like the TTFT/e2e timelines.

    plot_routing_timeline.py <run dir> -o out.png --label "smart (ttft-aware scorer)"
    plot_routing_timeline.py <smart dir> --random <random dir> -o out.png   # smart above, random below

Every shared (model:"auto") request is a tick in the colour of the pool IPP picked, plus the
share sent to Gemma per BIN seconds. Shared vs pinned comes from the picker's `numCandidates`
(2 = both pools were eligible, 1 = the name filter had already pinned it), so aborted requests
count too -- unlike e2e.csv, which only has completions.

With --random both arms share one canvas and each stage slot is as wide as the slower arm, so a
stage sits directly above its counterpart (pairing from plot_arms_stacked.PAIRS).
"""
from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

GAP, BIN = 20.0, 5.0
STREAM_C = {"shared": "#1f77b4"}


def load_mod(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def decisions(raw_gz: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """-> (ts, is_gemma, was_shared) per request, in arrival order."""
    cand, sel = {}, {}
    with gzip.open(raw_gz, "rt", errors="ignore") as f:
        for line in f:
            if '"numCandidates"' not in line and '"Model selected"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            rid = d.get("x-request-id")
            if not rid:
                continue
            if "numCandidates" in d:
                cand[rid] = d["numCandidates"]
            elif d.get("msg") == "Model selected":
                sel[rid] = (d["ts"], "gemma" in d.get("model", ""))
    rows = sorted((t, g, cand.get(r, 0) == 2) for r, (t, g) in sel.items())
    ts, gem, sh = (np.array(c) for c in zip(*rows))
    return ts, gem, sh


def draw_slot(ax, x, dat, label_lines: bool) -> None:
    ts, gem, sh = dat
    rel = ts - ts.min()
    for mask, lw, ls, lbl in ((sh, 2.4, "-", 'shared model:"auto" requests'),
                              (np.ones_like(sh), 1.2, ":", "all requests (shared + pinned)")):
        bins = np.arange(0, rel[mask].max() + BIN, BIN)
        idx = np.digitize(rel[mask], bins)
        g = gem[mask]
        y = [100 * g[idx == k].mean() if (idx == k).any() else np.nan
             for k in range(1, len(bins) + 1)]
        ax.plot(x + bins, y, color="black", lw=lw, ls=ls, label=lbl if label_lines else None)
    # one tick per shared decision, at the top for Gemma and the bottom for Qwen
    for pick, y0 in ((True, 104), (False, -4)):
        m = sh & (gem == pick)
        ax.plot(x + rel[m], np.full(m.sum(), y0), "|", ms=5, alpha=0.10,
                color=STREAM_C["gemma"] if pick else STREAM_C["qwen"])
    share = 100 * gem[sh].mean()
    ax.annotate(f"{share:.1f}% Gemma\n{sh.sum()} decisions", (x + rel.max() / 2, share),
                fontsize=8, ha="center", va="center", bbox=dict(fc="white", ec="0.7",
                                                                alpha=0.85, pad=0.25))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="run dir (the smart arm when --random is given)")
    ap.add_argument("--random", help="second run dir, drawn in a bottom row")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--label", default="smart (ttft-aware scorer)")
    a = ap.parse_args()

    pas = load_mod("plot_adaptive_stages")
    STREAM_C.update(gemma=pas.GEMMA_C, qwen=pas.QWEN_C)

    def usable(root: Path, s: str) -> bool:
        return (root / s / "ipp-raw.log.gz").is_file() and (root / s / "shared").is_dir()

    roots = [Path(a.root)] + ([Path(a.random)] if a.random else [])
    if a.random:
        pairs = [p for p in load_mod("plot_arms_stacked").PAIRS
                 if all(usable(r, s) for r, s in zip(roots, p))]
    else:
        pairs = [(s,) for s in pas.STAGE_ORDER if usable(roots[0], s)]
    if not pairs:
        print(f"no stages with a shared stream + ipp-raw.log.gz under {a.root}", file=sys.stderr)
        return 1

    dat = {(i, si): decisions(r / p[i] / "ipp-raw.log.gz")
           for si, p in enumerate(pairs) for i, r in enumerate(roots)}

    names = [a.label, "random picker (all scores 0)"]
    fig, axes = plt.subplots(len(roots), 1, figsize=(3.1 * len(pairs) + 2, 5.2 * len(roots)),
                             squeeze=False, sharex=True, sharey=True)
    axes = [ax[0] for ax in axes]
    x = 0.0
    for si, p in enumerate(pairs):
        width = max(t.max() - t.min() for t, *_ in (dat[(i, si)] for i in range(len(roots))))
        for i, ax in enumerate(axes):
            draw_slot(ax, x, dat[(i, si)], label_lines=(si == 0))
            ax.axvspan(x, x + width, color="black", alpha=0.03)
        lf = pas.lifecycle(str(roots[0] / p[0]))
        rps = [(k, lf[k]["rate"]) for k in ("gemma", "qwen", "shared") if k in lf]
        for j, (stream, r) in enumerate(rps):        # stack upward, never into the plot
            axes[0].text(x + width / 2, 1.02 + 0.052 * (len(rps) - 1 - j) / len(roots),
                         f"{stream} {r:.0f} rps", transform=axes[0].get_xaxis_transform(),
                         ha="center", va="bottom", fontsize=8.5, color=STREAM_C[stream])
        axes[0].text(x + width / 2, 1.04 + 0.052 * len(rps) / len(roots),
                     " / ".join(dict.fromkeys(p)), transform=axes[0].get_xaxis_transform(),
                     ha="center", va="bottom", fontsize=9, fontweight="bold")
        x += width + GAP
    for i, ax in enumerate(axes):
        ax.axhline(50, color="0.6", lw=1, ls="--")
        ax.set_ylim(-12, 112)
        ax.set_yticks([0, 25, 50, 75, 100])
        ax.set_yticklabels(["all Qwen", "25%", "50/50", "75%", "all Gemma"], fontsize=8)
        ax.set_ylabel("share of requests routed to Gemma", fontsize=9)
        ax.grid(alpha=0.3, axis="y")
        if len(roots) > 1:      # a title on the top row would collide with the stage labels
            ax.text(0.995, 0.965, names[i], transform=ax.transAxes, ha="right", va="top",
                    fontsize=12, fontweight="bold",
                    bbox=dict(fc="white", ec="0.7", alpha=0.9, pad=0.4))
    axes[0].set_xlim(-GAP / 2, x - GAP / 2)
    axes[0].legend(fontsize=8, loc="center left", framealpha=0.95)
    axes[-1].set_xlabel("elapsed time across the experiment (s), stages laid end to end"
                        + ("; each stage's slot is as wide as the slower arm" if a.random else ""),
                        fontsize=9)
    fig.suptitle(f"IPP routing decisions over time - {a.label if not a.random else 'both routers'}\n"
                 f"share of requests sent to Gemma per {BIN:.0f} s; ticks top/bottom are single "
                 f"shared requests picked Gemma/Qwen", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.86 if len(roots) == 1 else 0.955))
    fig.savefig(a.out, dpi=140)
    print(f"wrote {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

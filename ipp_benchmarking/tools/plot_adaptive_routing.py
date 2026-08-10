#!/usr/bin/env python3
"""Routing-decision figures for the Gemma/Qwen adaptive experiment, drawn by
plot_experiment1.plot_traffic_share (imported unmodified).

    plot_adaptive_routing.py <run dir> -o <out dir> --label "smart (ttft-aware p25)"

    <out>/routing_shared.png  share of the shared model:"auto" stream sent to each pool
    <out>/routing_all.png     share of ALL requests (shared + pinned) landing on each pool

Counts come from the IPP "Model selected" line, so aborted requests are included: the
completed-only view in e2e.csv understates the imbalance (random s1 shared->Gemma reads
43.9% completed vs 50.7% routed). Every stream offers the same count, so the shared
split is each pool's total minus the pinned stream feeding it.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import numpy as np

STAGES = ["s-1_baseline1", "s0_baseline2", "s0_shared20", "s1_pinGemma", "s2_release",
          "s3_pinQwen", "s4_both", "s5_release", "s6_shared20"]
GEM, QW = "#7b3fa0", "#2e8b57"


def load_exp1():
    spec = importlib.util.spec_from_file_location(
        "plot_experiment1", Path(__file__).with_name("plot_experiment1.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.COLORS = {"gemma": GEM, "qwen": QW}
    mod.LABELS = {"gemma": "Gemma-4 26B pool", "qwen": "Qwen3.6 35B pool"}
    return mod


class Lbl(str):
    """plot_traffic_share formats the tick label as f"{rate:g}"; our stages all run at the
    same RPS, so carry the stage name through that format spec instead."""

    def __format__(self, spec):
        return str(self)


@contextlib.contextmanager
def retitled(title, xlabel):
    """plot_traffic_share hard-codes an 8B/32B title and an "RPS" x-label. Rewrite both as
    the figure is saved rather than editing the script."""
    real = matplotlib.figure.Figure.savefig

    def patched(self, *a, **k):
        ax = self.axes[0]
        ax.set_title(title, fontsize=10)
        ax.set_xlabel(xlabel)
        self.tight_layout()
        return real(self, *a, **k)

    matplotlib.figure.Figure.savefig = patched
    try:
        yield
    finally:
        matplotlib.figure.Figure.savefig = real


def routed(stage_dir: Path):
    """-> ({pool: routed count}, [stream names])"""
    n = {"gemma": 0, "qwen": 0}
    with open(stage_dir / "ipp-decisions.log", errors="replace") as f:
        for line in f:
            if '"msg":"Model selected"' not in line:
                continue
            n["gemma" if "gemma" in json.loads(line)["model"] else "qwen"] += 1
    streams = [s for s in ("gemma", "qwen", "shared") if (stage_dir / s).is_dir()]
    return n, streams


def stage_stats(root: Path, shared_only: bool):
    stats = []
    for s in STAGES:
        d = root / s
        if not (d / "e2e.csv").is_file():
            continue
        n, streams = routed(d)
        total = sum(n.values())
        per_stream, rem = divmod(total, len(streams))
        assert rem == 0, f"{s}: {total} decisions over {len(streams)} streams"
        if shared_only:
            if "shared" not in streams:
                continue
            n = {p: n[p] - per_stream * (p in streams) for p in n}
            assert sum(n.values()) == per_stream, f"{s}: shared split {n} != {per_stream}"
        stats.append(dict(stage=s, rate=Lbl(s.replace("_", "\n")), count=sum(n.values()),
                          by_model={p: np.zeros(c) for p, c in n.items()},
                          s_e=0.0, e_e=1.0, duration=1.0))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--label", required=True, help="arm name shown in the titles")
    args = ap.parse_args()

    exp1 = load_exp1()
    root, out = Path(args.root), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    arm = dict(name=args.label)

    for name, shared_only, title in (
        ("routing_shared", True,
         f'IPP routing decisions — shared model:"auto" stream — {args.label}\n'
         "share of the model-agnostic requests each stage sent to each pool "
         "(50% = no preference)"),
        ("routing_all", False,
         f"Load landing on each pool — shared + pinned — {args.label}\n"
         "what each GPU actually received per stage"),
    ):
        arm["stage_stats"] = stage_stats(root, shared_only)
        for s in arm["stage_stats"]:
            g, q = (len(s["by_model"][p]) for p in ("gemma", "qwen"))
            print(f"  {s['stage']:14s} gemma {g:5d}  qwen {q:5d}  "
                  f"({100 * g / max(g + q, 1):.1f}% / {100 * q / max(g + q, 1):.1f}%)")
        with retitled(title, "stage"):
            exp1.plot_traffic_share(arm, out / f"{name}.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())

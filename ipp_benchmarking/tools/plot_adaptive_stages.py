#!/usr/bin/env python3
"""Per-stage analysis for the Gemma/Qwen adaptive-routing experiment.

    plot_adaptive_stages.py <stage-by-stage-final dir> -o <out-stem>

Sibling of plot_ttft_actual_vs_predicted.py, which cannot be used here: that one keys on
`"queue-ttft score"` / concurrency sweeps, whereas the ttft-aware-scorer emits
`"ttft-aware score"` and this experiment is a 7-stage toggle timeline at fixed RPS.

Inputs, all already archived per stage:
  <stage>/ipp-decisions.log        PREDICTED  "ttft-aware score"   -> effectiveTTFT, score, trusted
                                   ACTUAL     "ttft-observation"   -> ttft_s, inflightAtDispatch
                                   ROUTING    "Model selected"     -> chosen pool
  <stage>/*/stage_0_lifecycle_metrics.json    harness-side e2e / TTFT percentiles per stream
  stage_marks.log                  stage windows (start_ts/end_ts)

Predicted and actual share the IPP wall-clock `ts` AND the x-request-id, so they join
per-request -- no time-binning approximation needed for the scatter.

Figures:
  <out-stem>_ttft_timeseries.png       one row per stage: actual vs predicted TTFT over time
  <out-stem>_e2e_<stage>.png           one file per stage: measured e2e latency distribution
and prints a text table of the same numbers.
"""
from __future__ import annotations

import argparse
import glob
import json
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

GEMMA_KEY, QWEN_KEY = "gemma", "Qwen"
GEMMA_C, QWEN_C = "#7b3fa0", "#2e8b57"   # purple / green, matching the other OCP plots
STAGE_ORDER = ["s-1_baseline1", "s0_baseline2", "s1_pinGemma", "s2_release",
               "s3_pinQwen", "s4_both", "s5_release",
               "s0_shared20", "s6_shared20"]   # 20 rps shared-only: shown last, not in run order


def pool_of(line: str) -> str | None:
    if GEMMA_KEY in line:
        return "gemma"
    if QWEN_KEY in line:
        return "qwen"
    return None


def read_stage(path: str) -> dict:
    """Parse one stage's ipp-decisions.log into per-request predicted/actual/routing."""
    pred: dict[str, dict[str, float]] = {}      # rid -> {pool: effectiveTTFT}
    trusted: dict[str, dict[str, bool]] = {}
    actual: dict[str, tuple[float, float, str]] = {}   # rid -> (ttft_s, inflight, pool)
    chosen: dict[str, str] = {}
    ts: dict[str, float] = {}
    for line in open(path, errors="ignore"):
        rid_m = re.search(r'"x-request-id":"([^"]+)"', line)
        ts_m = re.search(r'"ts":([0-9.]+)', line)
        if not rid_m or not ts_m:
            continue
        rid, t = rid_m.group(1), float(ts_m.group(1))
        if '"msg":"ttft-aware score"' in line:
            mo = re.search(r'"model":"([^"]+)"', line)
            # queue-ttft-scorer logs effectiveTTFT; ttft-aware-scorer logs predictedTTFT
            et = re.search(r'"(?:effectiveTTFT|predictedTTFT)":([0-9.eE+-]+)', line)
            tr = re.search(r'"trusted":(true|false)', line)
            if mo and et:
                p = pool_of(mo.group(1))
                if p:
                    pred.setdefault(rid, {})[p] = float(et.group(1))
                    if tr:
                        trusted.setdefault(rid, {})[p] = tr.group(1) == "true"
        elif '"msg":"ttft-observation"' in line:
            mo = re.search(r'"model":"([^"]+)"', line)
            tt = re.search(r'"ttft_s":([0-9.eE+-]+)', line)
            infl = re.search(r'"inflightAtDispatch":([0-9]+)', line)
            if mo and tt:
                p = pool_of(mo.group(1))
                if p:
                    actual[rid] = (float(tt.group(1)), float(infl.group(1)) if infl else float("nan"), p)
        elif '"msg":"Model selected"' in line:
            p = pool_of(line)
            if p:
                chosen[rid] = p
                ts[rid] = t
    return dict(pred=pred, actual=actual, chosen=chosen, ts=ts, trusted=trusted)


def paired(st: dict) -> dict[str, list[tuple[float, float, float, float, bool]]]:
    """Join predicted(selected pool) with actual, per pool: (t, pred, actual, inflight, trusted)."""
    out: dict[str, list] = {"gemma": [], "qwen": []}
    for rid, (a_ttft, infl, a_pool) in st["actual"].items():
        p = st["pred"].get(rid, {}).get(a_pool)
        if p is None:
            continue
        tr = st["trusted"].get(rid, {}).get(a_pool, True)
        out[a_pool].append((st["ts"].get(rid, 0.0), p, a_ttft, infl, tr))
    for k in out:
        out[k].sort()
    return out


def lifecycle(stage_dir: str) -> dict[str, dict]:
    """Harness-side percentiles per stream (shared / gemma / qwen sub-dirs)."""
    res = {}
    for sub in ("shared", "gemma", "qwen"):
        for f in glob.glob(os.path.join(stage_dir, sub, "stage_0_lifecycle_metrics.json")):
            d = json.load(open(f))
            ok = d.get("successes") or {}
            lat = ok.get("latency") or {}
            res[sub] = dict(
                rl=lat.get("request_latency") or {},
                tt=lat.get("time_to_first_token") or {},
                n=ok.get("count", 0),
                fail=(d.get("failures") or {}).get("count", 0),
                rate=d["load_summary"].get("requested_rate"),
                dur=d.get("benchmark_time_seconds", 180.0),
            )
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="stage-by-stage-final directory")
    ap.add_argument("-o", "--out", default="adaptive", help="output stem")
    args = ap.parse_args()

    stages = [s for s in STAGE_ORDER
              if os.path.isfile(os.path.join(args.root, s, "ipp-decisions.log"))]
    if not stages:
        print(f"no stages with ipp-decisions.log under {args.root}", file=sys.stderr)
        return 1

    data = {s: read_stage(os.path.join(args.root, s, "ipp-decisions.log")) for s in stages}
    pairs = {s: paired(data[s]) for s in stages}
    lifes = {s: lifecycle(os.path.join(args.root, s)) for s in stages}

    # ---------- text summary ----------
    print(f"{'stage':<16} {'pool':<6} {'n':>6} {'pred_med':>9} {'act_med':>9} "
          f"{'MAE':>8} {'bias':>8} {'corr':>6}")
    summary = {}
    for s in stages:
        for pool in ("gemma", "qwen"):
            v = pairs[s][pool]
            if len(v) < 5:
                continue
            _, pr, ac, _, _ = map(np.array, zip(*v))
            mae = float(np.mean(np.abs(pr - ac)))
            bias = float(np.mean(pr - ac))
            corr = float(np.corrcoef(pr, ac)[0, 1]) if len(pr) > 2 and pr.std() > 0 and ac.std() > 0 else float("nan")
            summary[(s, pool)] = (len(v), float(np.median(pr)), float(np.median(ac)), mae, bias, corr)
            print(f"{s:<16} {pool:<6} {len(v):6d} {np.median(pr):9.4f} {np.median(ac):9.4f} "
                  f"{mae:8.4f} {bias:+8.4f} {corr:6.3f}")

    # ---------- figure 1: TTFT actual vs predicted, time series, one row per stage ----------
    n = len(stages)
    BIN = 10.0
    fig, axes = plt.subplots(n, 1, figsize=(11, 2.05 * n), squeeze=False, sharex=True)
    for j, s in enumerate(stages):
        ax = axes[j][0]
        t0 = min([t for t, *_ in sum(pairs[s].values(), [])] or [0])
        for pool, c in (("gemma", GEMMA_C), ("qwen", QWEN_C)):
            v = pairs[s][pool]
            if len(v) < 5:
                continue
            t, pr, ac, _, tr = map(np.array, zip(*v))
            rel = t - t0
            bins = np.arange(0, rel.max() + BIN, BIN)
            idx = np.digitize(rel, bins)
            med = lambda a, m: [np.median(a[(idx == k) & m]) if ((idx == k) & m).any() else np.nan
                                for k in range(1, len(bins) + 1)]
            ax.plot(bins, med(ac, np.ones_like(tr, bool)), color=c, lw=2.2,
                    label=f"{pool} actual")
            # predicted is only meaningful where the pool's estimate was trusted (had >=5 recent
            # observations); untrusted picks use a deliberately optimistic exploration fallback
            ax.plot(bins, med(pr, tr.astype(bool)), color=c, lw=1.4, ls="--",
                    marker="o", ms=3, label=f"{pool} predicted")
        # a shared (model:"auto") request has BOTH pools scored; a pinned one has exactly one
        sh = [p for rid, p in data[s]["chosen"].items() if len(data[s]["pred"].get(rid, {})) == 2]
        ttl = s if not sh else (f"{s}   -   shared stream ({len(sh)} req): "
                               f"{100 * sh.count('gemma') / len(sh):.0f}% Gemma / "
                               f"{100 * sh.count('qwen') / len(sh):.0f}% Qwen")
        ax.set_title(ttl, fontsize=10, loc="left")
        ax.set_yscale("log")
        ax.set_ylim(0.015, 8)
        ax.set_yticks([0.02, 0.1, 0.5, 3.0])
        ax.set_yticklabels(["20 ms", "100 ms", "500 ms", "3 s"], fontsize=8)
        ax.set_ylabel("TTFT", fontsize=9)
        ax.grid(alpha=0.3, which="major")
        if j == 0:
            ax.legend(fontsize=8, ncol=4, loc="upper right", framealpha=0.95)
    axes[-1][0].set_xlabel("seconds into stage", fontsize=9)
    fig.suptitle("TTFT over time: measured (solid) vs what the ttft-aware scorer predicted (dashed)\n"
                 "medians per 10 s bin - lines close together = scorer well calibrated", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.965))
    f1 = f"{args.out}_ttft_timeseries.png"
    fig.savefig(f1, dpi=130); plt.close(fig)

    # ---------- figure 2..N: measured e2e latency, one file per stage ----------
    PCTS = [("p10", 10), ("p25", 25), ("median", 50), ("p75", 75), ("p90", 90), ("p99", 99)]
    styles = {"shared": ("#1f77b4", "o"), "gemma": (GEMMA_C, "s"), "qwen": (QWEN_C, "^")}
    written = []
    for s in stages:
        fig, ax = plt.subplots(figsize=(7.6, 4.6))
        for i, (stream, (c, mk)) in enumerate(styles.items()):
            rl = lifes[s].get(stream, {}).get("rl") or {}
            ys = [rl.get(k, np.nan) for k, _ in PCTS]
            if not any(np.isfinite(y) for y in ys):
                continue
            m = lifes[s][stream]
            ax.plot([x for _, x in PCTS], ys, color=c, marker=mk, lw=2.0,
                    label=f"{stream}  ({m['rate']:.0f} rps, n={m['n']}, {m['fail']} failed)")
            ax.annotate(f"{rl['median']:.2f}s", (50, rl["median"]), textcoords="offset points",
                        xytext=(0, 8 + 11 * i), fontsize=8, color=c, ha="center")
            ax.annotate(f"{rl['p99']:.1f}s", (99, rl["p99"]), textcoords="offset points",
                        xytext=(-4, 6), fontsize=8, color=c, ha="right")
        ax.set_xticks([x for _, x in PCTS])
        ax.set_xticklabels([k for k, _ in PCTS], fontsize=9)
        ax.set_xlabel("percentile of requests", fontsize=9)
        ax.set_ylabel("end-to-end request latency (s)", fontsize=9)
        ax.set_title(f"{s}: measured end-to-end latency per stream", fontsize=11)
        ax.grid(alpha=0.3)
        ax.legend(fontsize=8, loc="upper left")
        fig.tight_layout()
        f = f"{args.out}_e2e_{s}.png"
        fig.savefig(f, dpi=130); plt.close(fig)
        written.append(f)

    # ---------- final figures: per-request e2e per MODEL, all stages on one timeline ----------
    # Per-request e2e comes from e2e.csv (derived from the IPP capture: last response chunk minus
    # request headers) so every request is attributed to the pool that served it -- pinned and
    # shared traffic pooled together, one line per model. A tail percentile needs far more samples
    # per bin than a median before it means anything, hence minbin.
    GAP, BIN2 = 20.0, 30.0
    # per-request samples per stage per pool: (arrival ts, value)
    pts = {"e2e": {}, "ttft": {}}
    for s in stages:
        f = os.path.join(args.root, s, "e2e.csv")
        if os.path.isfile(f):
            rows = [l.split(",") for l in open(f).read().splitlines()[1:] if l]
            pts["e2e"][s] = {p: np.array([(float(r[2]), float(r[4])) for r in rows if r[1] == p])
                             for p in ("gemma", "qwen")}
        st = data[s]
        pts["ttft"][s] = {p: np.array([(st["ts"][r], tt) for r, (tt, _, pl) in st["actual"].items()
                                       if pl == p and r in st["ts"]])
                          for p in ("gemma", "qwen")}

    tls = []
    # e2e is linear; TTFT spans 0.04 s -> 4.9 s, where a linear axis flattens the whole baseline
    for metric, ylab, pct, minbin, logy in (("e2e", "end-to-end latency (s)", 50, 8, False),
                                            ("e2e", "end-to-end latency (s)", 95, 30, False),
                                            ("ttft", "time to first token (s)", 50, 8, True),
                                            ("ttft", "time to first token (s)", 95, 30, True)):
        fig, ax = plt.subplots(figsize=(15, 5.6))
        x, peak = 0.0, 0.0
        for s in stages:
            by_pool = pts[metric].get(s) or {}
            allt = np.concatenate([v[:, 0] for v in by_pool.values() if len(v)] or [np.zeros(1)])
            t0, dur = allt.min(), allt.max() - allt.min()
            overall = {}
            for pool, c in (("gemma", GEMMA_C), ("qwen", QWEN_C)):
                v = by_pool.get(pool, np.empty((0, 2)))
                if len(v) < minbin:
                    continue
                v = np.column_stack([v[:, 0] - t0, v[:, 1]])
                if len(v) >= 3 * minbin:      # a stage-wide p95 off ~30 samples is meaningless too
                    overall[pool] = (float(np.percentile(v[:, 1], pct)), c)
                ax.scatter(x + v[:, 0], v[:, 1], s=2.5, alpha=0.12, color=c, edgecolors="none")
                bins = np.arange(0, v[:, 0].max() + BIN2, BIN2)
                idx = np.digitize(v[:, 0], bins)
                y = [np.percentile(v[idx == k, 1], pct) if (idx == k).sum() >= minbin else np.nan
                     for k in range(1, len(bins) + 1)]
                ax.plot(x + bins, y, color=c, lw=2.4)
                peak = max(peak, float(np.nanmax(y)))
            for i, (pool, (m, c)) in enumerate(sorted(overall.items(), key=lambda kv: -kv[1][0])):
                dy = (9, 22) if logy else (9, -15)     # log baseline sits on the axis: stack above
                ax.annotate(f"p{pct} {m:.3f}s" if m < 1 else f"p{pct} {m:.2f}s", (x + 6, m),
                            fontsize=8, color=c, ha="left", textcoords="offset points",
                            xytext=(0, dy[i]), bbox=dict(fc="white", ec="none", alpha=0.75, pad=0.15))
            ax.axvspan(x, x + dur, color="black", alpha=0.03)
            rps = [(st, lifes[s][st]["rate"], styles[st][0]) for st in styles if st in lifes[s]]
            for i, (st, r, c) in enumerate(rps):                 # stack upward, never into the plot
                ax.text(x + dur / 2, 1.02 + 0.045 * (len(rps) - 1 - i), f"{st} {r:.0f} rps",
                        transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                        fontsize=8, color=c)
            ax.text(x + dur / 2, 1.04 + 0.045 * len(rps), s, transform=ax.get_xaxis_transform(),
                    ha="center", va="bottom", fontsize=9, fontweight="bold")
            x += dur + GAP
        for pool, c in (("gemma", GEMMA_C), ("qwen", QWEN_C)):
            ax.plot([], [], color=c, lw=2.4, label=f"{pool}: p{pct} of every request routed there")
        ax.set_xlim(-GAP / 2, x - GAP / 2)
        if logy:
            ax.set_yscale("log")
            ax.set_ylim(0.02, peak * 3)
            ax.set_yticks([0.02, 0.05, 0.1, 0.25, 0.5, 1, 2, 5])
            ax.set_yticklabels(["20 ms", "50 ms", "100 ms", "250 ms", "500 ms", "1 s", "2 s", "5 s"],
                               fontsize=8)
        else:
            ax.set_ylim(0, peak * 1.35)      # linear: slower individual requests run off the top
        ax.set_xlabel("elapsed time across the experiment (s), stages laid end to end", fontsize=9)
        ax.set_ylabel(ylab, fontsize=9)
        ax.grid(alpha=0.3, axis="y")
        ax.legend(fontsize=9, loc="upper left", ncol=2)
        fig.suptitle(f"Per-request {ylab.split(' (')[0]} by serving model - dots are single requests, "
                     f"line is the p{pct} per {BIN2:.0f} s"
                     + ("" if logy else
                        f" (linear axis: dots above {peak * 1.35:.1f} s are off the chart)"),
                     fontsize=12)
        fig.tight_layout()
        f_tl = f"{args.out}_{metric}_p{pct}_timeline.png"
        fig.savefig(f_tl, dpi=140); plt.close(fig)
        tls.append(f_tl)

    print("\nwrote:\n  " + "\n  ".join([f1] + tls + written))
    return 0


if __name__ == "__main__":
    sys.exit(main())

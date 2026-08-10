#!/usr/bin/env python3
"""Three-(or N-)way IPP picker comparison at saturation, aggregating -j N pods.

Each LABEL=collected-logs-N is a run whose benchmark-results/results/ holds one
sub-dir per parallel harness pod (`<exp>_1.._N`). Per load stage we SUM across
pods: achieved_rate (-> aggregate RPS, the x-axis), successes, failures, and
output_tokens_per_sec; p95 latency and TTFT are averaged across pods.

Usage:
    plot_picker_compare.py RANDOM=collected-logs-49 TTFT=collected-logs-50 \
        INFLIGHT=collected-logs-51 -o picker_compare.png
"""
import argparse
import glob
import json
import os
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402


def aggregate(logs_dir):
    base = os.path.join(logs_dir, "benchmark-results")
    pod_dirs = sorted({
        os.path.dirname(f)
        for f in glob.glob(base + "/**/stage_0_lifecycle_metrics.json", recursive=True)
    })
    # discover load stages (skip cooldown stages: requested_rate < 1)
    rows = []
    # stage indices present in pod 0
    stage_files = sorted(glob.glob(os.path.join(pod_dirs[0], "stage_*_lifecycle_metrics.json")))
    stages = sorted(int(os.path.basename(f).split("_")[1]) for f in stage_files)
    for st in stages:
        ach = ok = fail = otps = 0.0
        p95s, ttfts = [], []
        req_rate = None
        for pd in pod_dirs:
            f = os.path.join(pd, f"stage_{st}_lifecycle_metrics.json")
            if not os.path.exists(f):
                continue
            d = json.load(open(f))
            ls = d["load_summary"]
            req_rate = ls.get("requested_rate")
            if (req_rate or 0) < 1:  # cooldown stage
                break
            s = d.get("successes", {}) or {}
            fl = d.get("failures", {}) or {}
            ach += ls.get("achieved_rate", 0) or 0
            ok += s.get("count", 0) or 0
            fail += fl.get("count", 0) or 0
            lat = (s.get("latency", {}) or {})
            tp = (s.get("throughput", {}) or {})
            otps += tp.get("output_tokens_per_sec", 0) or 0
            rl = lat.get("request_latency", {}) or {}
            tt = lat.get("time_to_first_token", {}) or {}
            if rl.get("p95") is not None:
                p95s.append(rl["p95"])
            if tt.get("mean") is not None:
                ttfts.append(tt["mean"])
        if (req_rate or 0) < 1:
            continue
        tot = ok + fail
        am = lambda xs: sum(xs) / len(xs) if xs else float("nan")
        rows.append({
            "rps": round(ach),
            "fail_pct": 100 * fail / tot if tot else 0,
            "p95": am(p95s),
            "ttft": am(ttfts),
            "otps": otps,
            "ok": int(ok),
            "fail": int(fail),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="LABEL=collected-logs-N entries")
    ap.add_argument("-o", "--output", default="picker_compare.png")
    args = ap.parse_args()

    runs = {}
    for entry in args.inputs:
        label, path = entry.split("=", 1)
        runs[label] = aggregate(path)

    fig, axes = plt.subplots(1, 3, figsize=(16, 5))
    panels = [
        ("fail_pct", "Failure rate (%)  — lower is better", "Request failures (60s timeouts)"),
        ("p95", "p95 request latency (s)  — lower is better", "Tail latency"),
        ("otps", "Output tokens/s  — higher is better", "Throughput"),
    ]
    colors = {"RANDOM": "#d62728", "TTFT": "#2ca02c", "INFLIGHT": "#1f77b4"}
    for ax, (key, ylabel, title) in zip(axes, panels):
        for label, rows in runs.items():
            xs = [r["rps"] for r in rows]
            ys = [r[key] for r in rows]
            ax.plot(xs, ys, "o-", label=label, color=colors.get(label), linewidth=2, markersize=7)
            for x, y in zip(xs, ys):
                ax.annotate(f"{y:.0f}" if key != "fail_pct" else f"{y:.1f}%",
                            (x, y), textcoords="offset points", xytext=(0, 7), fontsize=8)
        ax.set_xlabel("Aggregate offered load (RPS)")
        ax.set_ylabel(ylabel)
        ax.set_title(title)
        ax.grid(True, alpha=0.3)
        ax.legend()
    fig.suptitle("IPP picker comparison at saturation (single-stack, -j4, shareGPT)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(args.output, dpi=130)
    print(f"wrote {args.output}")

    # also emit a CSV-ish table to stdout
    print("\npicker,rps,fail_pct,p95_s,ttft_s,out_tok_s,ok,fail")
    for label, rows in runs.items():
        for r in rows:
            print(f"{label},{r['rps']},{r['fail_pct']:.1f},{r['p95']:.2f},"
                  f"{r['ttft']:.2f},{r['otps']:.0f},{r['ok']},{r['fail']}")


if __name__ == "__main__":
    sys.exit(main())

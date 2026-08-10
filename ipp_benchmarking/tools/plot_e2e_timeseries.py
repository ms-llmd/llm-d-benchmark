#!/usr/bin/env python3
"""8B e2e latency vs stage progress, pinned (8B-only) vs auto (offload to 8B+32B) on ONE axes.
p50 (median) for each run, overlaid. Grey bands = the 8B ladder stages (RPS auto-derived from the
stage lifecycle files). Red ticks = the auto run's 32B background active windows.

The two runs are SEPARATE and pace differently (a saturated pinned loadgen falls behind schedule,
so its stages drift minutes later than auto's). Plotting real elapsed seconds therefore misaligns
identical RPS stages. So each request is warped through its OWN run's stage windows to a fractional
stage index (0..N); both curves' stage N then land inside band N regardless of drift.

  plot_e2e_timeseries.py <base_dir> [--pinned dualfilter] [--auto auto] [--bin 5] [-o out.png]
"""
import argparse, glob, json, re, datetime
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

STAGE_RE = re.compile(r"([0-9-]+ [0-9:,]+).*Stage ([0-9]+) - run (started|completed)")


def wall(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def stage_windows(path):
    """[(stage, start, end)] in wall-clock seconds, from harness stdout."""
    st, en = {}, {}
    for line in open(path, errors="replace"):
        m = STAGE_RE.search(line)
        if not m:
            continue
        (st if m.group(3) == "started" else en)[int(m.group(2))] = wall(m.group(1))
    return [(n, st[n], en.get(n, st[n])) for n in sorted(st)]


def stage_elapsed(path):
    """Stage windows reduced to elapsed-from-Stage-0 seconds."""
    w = stage_windows(path)
    s0 = w[0][1]
    return [(n, s - s0, e - s0) for (n, s, e) in w]


def to_stage_x(elapsed, W):
    """Map elapsed-from-Stage-0 seconds -> fractional stage index using windows W=[(n,S,E)]."""
    ns = np.array([w[0] for w in W]); Ss = np.array([w[1] for w in W]); Es = np.array([w[2] for w in W])
    idx = np.clip(np.searchsorted(Ss, elapsed, side="right") - 1, 0, len(W) - 1)
    frac = np.clip((elapsed - Ss[idx]) / np.maximum(Es[idx] - Ss[idx], 1e-9), 0, 1)
    return ns[idx] + frac


def rps_ladder(d):
    out = {}
    for f in glob.glob(f"{d}/stage_*_lifecycle_metrics.json"):
        n = int(f.split("stage_")[1].split("_")[0])
        out[n] = json.load(open(f))["load_summary"]["requested_rate"]
    return [out[k] for k in sorted(out)]


def p50_series(slim, stages_dir, bin_s):
    """Median e2e latency vs fractional stage index; each run warped by its own stage windows."""
    d = json.load(open(slim))
    t = np.array([r["t"] for r in d if not r["fail"]])
    lat = np.array([r["lat"] for r in d if not r["fail"]])
    o = np.argsort(t); t, lat = t[o], lat[o]
    W = stage_elapsed(f"{stages_dir}/harness_stdout.log")
    sx = to_stage_x(t - t.min(), W)
    step = bin_s / 300.0                       # ~bin_s seconds within a nominal 300s stage
    b = (sx / step).astype(int); ks = np.unique(b)
    return ks * step, np.array([np.median(lat[b == k]) for k in ks])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--pinned", default="dualfilter")
    ap.add_argument("--auto", default="auto")
    ap.add_argument("--bin", type=float, default=5.0)
    ap.add_argument("-o", "--output", default=None)
    a = ap.parse_args()
    P, A = f"{a.base}/{a.pinned}", f"{a.base}/{a.auto}"
    out = a.output or f"{a.base}/e2e_timeseries_8b_pinned_vs_auto.png"

    RPS = rps_ladder(f"{A}/8b")
    N = len(RPS)
    aW = stage_elapsed(f"{A}/8b/harness_stdout.log")   # auto 8B windows, for the 32B ticks warp

    fig, ax = plt.subplots(figsize=(13, 6))
    for n in range(N):
        ax.axvspan(n, n + 1, color="0.90" if n % 2 == 0 else "0.83", zorder=0)
        ax.text(n + 0.5, 0.96, f"{RPS[n]:g}", transform=ax.get_xaxis_transform(),
                ha="center", va="top", fontsize=8, color="dimgray")
    xp, yp = p50_series(f"{P}/8b/per_request_slim.json", f"{P}/8b", a.bin)
    xa, ya = p50_series(f"{A}/8b/per_request_slim.json", f"{A}/8b", a.bin)
    ax.plot(xp, yp, "-", color="#9467bd", lw=1.6, label="pinned (8B only)")
    ax.plot(xa, ya, "-", color="#2ca02c", lw=1.6, label="auto (offload 8B->32B)")
    for (_, s, e) in stage_windows(f"{A}/32b/harness_stdout.log"):   # 32B bg, warped like the auto curve
        s0 = stage_windows(f"{A}/8b/harness_stdout.log")[0][1]
        x0, x1 = to_stage_x(np.array([s - s0, (e or s) - s0]), aW)
        ax.axvspan(x0, x1, ymin=0.010, ymax=0.045, color="#d62728", alpha=0.6, zorder=3)

    ax.set_yscale("log")
    ax.set_xlim(-0.3, N + 0.3)
    ax.set_xlabel("8B stage progress (RPS ladder — each run warped to its own stage windows)")
    ax.set_ylabel("8B e2e latency (s, log)")
    ax.set_title(f"8B e2e latency by stage (median/{a.bin:g}s): pinned vs auto-offload")
    ax.grid(alpha=.3, which="both")
    handles = [plt.Line2D([], [], color="#9467bd", lw=2, label="pinned (8B only)"),
               plt.Line2D([], [], color="#2ca02c", lw=2, label="auto (offload 8B->32B)"),
               Patch(facecolor="#d62728", alpha=0.6, label="32B background active (auto run)")]
    ax.legend(handles=handles, fontsize=9, loc="upper left", framealpha=0.95)
    fig.tight_layout(); fig.savefig(out, dpi=130)
    print("wrote", out)
    print(f"pinned peak p50={yp.max():.1f}s  auto peak p50={ya.max():.1f}s")


if __name__ == "__main__":
    main()


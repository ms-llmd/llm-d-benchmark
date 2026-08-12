#!/usr/bin/env python3
"""Two or more runs' per-request e2e latency on one axes, for arm-vs-arm comparison.

    plot_e2e_compare.py <run-dir> <run-dir> [...] --labels smart,random [--bin 15] [--log]

The x axis is fractional stage index, not elapsed seconds. Arms that fall behind
stretch their stages (a collapsing arm took 533s against the other's 426s), so
plotting real time would slide identical RPS rungs out of alignment. Each run is
warped through its OWN stage windows, so every run's stage N lands in band N.
Medians are still computed over real `--bin` second windows, then warped.

Colour is per RUN. Colouring per leaf is not possible from harness data: the
records carry no upstream identity and both leaves serve the same model name.
Use the per-leaf EPP gauges for that view.
"""
import argparse, datetime, glob, json, os, re, sys, tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

STAGE_RE = re.compile(r"([0-9-]+ [0-9:,]+).*Stage ([0-9]+) - run (started|completed)")
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]


def _wall(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def stage_windows(run, cap=None):
    """[(stage, start, end)] in seconds elapsed from stage 0.

    "run completed" is logged when the harness moves on, not when the last
    response lands, and it can trail by hours if the run stalls afterwards --
    one baseline arm logged stage 1 as 14857s long against 407s of requests.
    Left uncorrected that stretches the stage so far that every warped point
    piles up on the boundary. So each stage ends no later than the next one
    starts, and the last no later than `cap`, the final request time.
    """
    st, en = {}, {}
    for line in open(f"{run}/stdout.log", errors="replace"):
        m = STAGE_RE.search(line)
        if m:
            (st if m.group(3) == "started" else en)[int(m.group(2))] = _wall(m.group(1))
    t0 = st[min(st)]
    ns = sorted(st)
    out = []
    for i, n in enumerate(ns):
        end = en.get(n, st[n])
        if i + 1 < len(ns):
            end = min(end, st[ns[i + 1]])
        end -= t0
        if i + 1 == len(ns) and cap is not None:
            end = min(end, cap)
        out.append((n, st[n] - t0, max(end, st[n] - t0)))
    return out


def stage_labels(run):
    """{stage: label}. Only open-loop stages report a rate; a `type: concurrent`
    stage puts num_requests in requested_rate, so it is labelled by concurrency.
    """
    out = {}
    for f in glob.glob(f"{run}/stage_*_lifecycle_metrics.json"):
        n = int(f.split("stage_")[1].split("_")[0])
        s = json.load(open(f))["load_summary"]
        out[n] = (f"{s['concurrency']:g} concurrent" if s.get("concurrency")
                  else f"{s['requested_rate']:g} RPS")
    return out


def to_stage_x(elapsed, W):
    """elapsed seconds -> fractional stage index, using this run's own windows."""
    ns = np.array([w[0] for w in W], float)
    Ss = np.array([w[1] for w in W], float)
    Es = np.array([w[2] for w in W], float)
    idx = np.clip(np.searchsorted(Ss, elapsed, side="right") - 1, 0, len(W) - 1)
    frac = np.clip((elapsed - Ss[idx]) / np.maximum(Es[idx] - Ss[idx], 1e-9), 0, 1)
    return ns[idx] + frac


def find_run_dir(base):
    if os.path.exists(f"{base}/stdout.log"):
        return base
    subs = [d for d in glob.glob(f"{base}/*") if os.path.exists(f"{d}/stdout.log")]
    if len(subs) != 1:
        sys.exit(f"expected one run dir under {base}, found {len(subs)}")
    return subs[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--labels", default=None, help="comma-separated, one per run")
    ap.add_argument("--bin", type=float, default=15.0)
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--title", default=None)
    ap.add_argument("-o", "--output", default="e2e_compare.png")
    a = ap.parse_args()

    runs = [find_run_dir(r) for r in a.runs]
    labels = (a.labels.split(",") if a.labels else [os.path.basename(r) for r in runs])
    if len(labels) != len(runs):
        sys.exit("--labels count must match the number of runs")

    fig, ax = plt.subplots(figsize=(13, 6))

    labels_by_stage = stage_labels(runs[0])
    nstages = max(len(stage_windows(r)) for r in runs)
    for n in range(nstages):
        ax.axvspan(n, n + 1, color="0.90" if n % 2 == 0 else "0.83", zorder=0)
        if n in labels_by_stage:
            ax.text(n + 0.5, 0.97, labels_by_stage[n], transform=ax.get_xaxis_transform(),
                    ha="center", va="top", fontsize=9, color="dimgray")

    for i, (run, label) in enumerate(zip(runs, labels)):
        slim = f"{run}/per_request_slim.json"
        if not os.path.exists(slim):
            sys.exit(f"missing {slim}\nrun: extract_per_request_slim.py "
                     f"{run}/per_request_lifecycle_metrics.json {slim}")
        recs = [r for r in json.load(open(slim)) if not r["fail"]]
        t0 = min(r["t"] for r in recs)
        t = np.array([r["t"] - t0 for r in recs])
        lat = np.array([r["lat"] for r in recs])
        W = stage_windows(run, cap=t.max())
        c = COLORS[i % len(COLORS)]

        # Scale opacity to the point count: one setting cannot serve a 400-request
        # sim arm and an 11k-request GPU run.
        ax.scatter(to_stage_x(t, W), lat, s=3, color=c, linewidths=0, zorder=2,
                   alpha=min(0.5, max(0.05, 200 / len(recs))))

        b = (t / a.bin).astype(int)
        ks = np.unique(b)
        p50 = np.array([np.median(lat[b == k]) for k in ks])
        ax.plot(to_stage_x((ks + 0.5) * a.bin, W), p50, "-", color=c, lw=2.0, zorder=3,
                # The x axis is stage progress, so the run's duration -- often
                # the clearest difference between arms -- has to be stated.
                label=f"{label} — p50/{a.bin:g}s, peak {p50.max():.0f}s, {t.max():.0f}s run")
        print(f"{label}: {len(recs)} ok, p50 peak {p50.max():.1f}s, "
              f"mean {lat.mean():.1f}s, p90 {np.percentile(lat, 90):.1f}s")

    if a.log:
        ax.set_yscale("log")
    ax.set_xlim(-0.05, nstages + 0.05)
    ax.set_xlabel("stage progress (each run warped to its own stage windows)")
    ax.set_ylabel("end-to-end latency (s)")
    ax.set_title(a.title or "Per-request e2e latency over the RPS ladder")
    ax.grid(alpha=0.3, which="both")
    # Lower left: the run labels are long enough that an upper-left box hides the
    # first stage's RPS annotation.
    ax.legend(fontsize=9, loc="lower left", framealpha=0.95)
    fig.tight_layout()
    fig.savefig(a.output, dpi=130)
    print("wrote", a.output)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""TTFT p95 over time per arm, from vLLM's own histogram scrapes.

    plot_ttft_p95_timeseries.py <arm-dir> [...] --labels queue,ttft,random -o <outdir>

Why not per-request data: an exact per-request p95 needs
`per_request_lifecycle_metrics.json`, which this experiment does not have --
the queue arm's was truncated to 0 bytes when the harness was OOM-killed writing
it, and the other arms ran with `per_request: false` to stop that recurring. The
EPP log is no substitute either: only the ttft arm runs a
latency-observer-producer, so the queue and random arms log no TTFT at all.

What every arm does have is `vllm:time_to_first_token_seconds_bucket`, scraped
every ~16s. Differencing consecutive scrapes gives the requests that completed
in that interval, and the percentile is interpolated inside the bucket it falls
in. So this is **approximate**: resolution is the scrape interval, and accuracy
is bounded by vLLM's bucket edges, which are coarse above 10s. Use the harness
per-stage numbers for exact values; use this for shape over time.

**Scope caveat**: llmdbenchmark scrapes only the namespace passed to `-p`, so
these are the mc-a (2-pod) leaf alone, not the fleet. That is the leaf an even
split overloads, which is what makes the comparison interesting -- but it is not
comparable to the fleet-wide harness p95.
"""
import argparse, datetime, glob, os, re, sys, tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

BUCKET_RE = re.compile(r'vllm:time_to_first_token_seconds_bucket\{[^}]*le="([^"]+)"[^}]*\}\s+([0-9.e+]+)')
STAGE_RE = re.compile(r"([0-9-]+ [0-9:,]+).*Stage ([0-9]+) - run (started|completed)")
COLORS = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd"]


def _wall(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def stage_windows(run_dir):
    f = os.path.join(run_dir, "stdout.log")
    if not os.path.exists(f):
        return []
    st, en = {}, {}
    for line in open(f, errors="replace"):
        m = STAGE_RE.search(line)
        if m:
            (st if m.group(3) == "started" else en)[int(m.group(2))] = _wall(m.group(1))
    ns = sorted(st)
    out = []
    for i, n in enumerate(ns):
        e = en.get(n, st[n])
        if i + 1 < len(ns):
            e = min(e, st[ns[i + 1]])
        out.append((n, st[n], max(e, st[n])))
    return out


def stage_labels(run_dir):
    """{stage: 'N rps'} from the harness stage metrics."""
    import json
    out = {}
    for f in glob.glob(os.path.join(run_dir, "stage_*_lifecycle_metrics.json")):
        n = int(os.path.basename(f).split("stage_")[1].split("_")[0])
        s = json.load(open(f))["load_summary"]
        out[n] = (f"{s['concurrency']:g} conc" if s.get("concurrency")
                  else f"{s['requested_rate']:g} rps")
    return out


def read_scrape(path):
    """{le: cumulative_count} summed over every engine in one scrape."""
    h = {}
    for line in open(path, errors="replace"):
        m = BUCKET_RE.search(line)
        if m:
            le = float("inf") if m.group(1) == "+Inf" else float(m.group(1))
            h[le] = h.get(le, 0.0) + float(m.group(2))
    return h


def series(run_dir, pct):
    """[(epoch, percentile)] from differenced histograms, decode pods summed."""
    raw = os.path.join(run_dir, "metrics", "raw")
    files = glob.glob(os.path.join(raw, "*decode*_metrics.log"))
    if not files:
        sys.exit(f"{run_dir}: no decode scrapes under metrics/raw")
    by_ts = {}
    for f in files:
        m = re.search(r"_(\d{10})_metrics\.log$", f)
        if not m:
            continue
        ts = int(m.group(1))
        h = read_scrape(f)
        acc = by_ts.setdefault(ts, {})
        for le, v in h.items():
            acc[le] = acc.get(le, 0.0) + v

    out = []
    tss = sorted(by_ts)
    for a, b in zip(tss, tss[1:]):
        ha, hb = by_ts[a], by_ts[b]
        les = sorted(set(ha) | set(hb))
        # Cumulative-count deltas -> how many requests landed in each bucket.
        cum = [hb.get(le, 0.0) - ha.get(le, 0.0) for le in les]
        total = cum[-1] if cum else 0.0
        if total < 5:            # too few completions to quantile meaningfully
            continue
        target = pct * total
        prev_le, prev_c = 0.0, 0.0
        for le, c in zip(les, cum):
            if c >= target:
                if le == float("inf"):
                    val = prev_le
                else:
                    span = c - prev_c
                    frac = (target - prev_c) / span if span > 0 else 0.0
                    val = prev_le + frac * (le - prev_le)
                out.append((b, val))
                break
            prev_le, prev_c = le, c
    return out


def find_run(d):
    if os.path.exists(os.path.join(d, "metrics", "raw")):
        return d
    subs = [x for x in glob.glob(f"{d}/*") if os.path.exists(os.path.join(x, "metrics", "raw"))]
    if len(subs) != 1:
        sys.exit(f"expected one run dir under {d}, found {len(subs)}")
    return subs[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("arms", nargs="+")
    ap.add_argument("--labels", default=None)
    ap.add_argument("--pct", type=float, default=0.95)
    ap.add_argument("--per-stage", action="store_true")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("-o", "--outdir", required=True)
    a = ap.parse_args()

    labels = a.labels.split(",") if a.labels else [os.path.basename(x.rstrip("/")) for x in a.arms]
    if len(labels) != len(a.arms):
        sys.exit("--labels count must match the number of arm directories")
    os.makedirs(a.outdir, exist_ok=True)
    name = f"p{a.pct * 100:g}"

    runs = [find_run(d) for d in a.arms]
    data, wins = [], []
    for run, label in zip(runs, labels):
        s = series(run, a.pct)
        w = stage_windows(run)
        t0 = w[0][1] if w else (s[0][0] if s else 0)
        data.append(np.array([(ts - t0, v) for ts, v in s], dtype=float))
        wins.append([(n, st - t0, en - t0) for n, st, en in w])
        print(f"{label}: {len(s)} intervals, {len(w)} stages, "
              f"peak {name} {max((v for _, v in s), default=0):.2f}s")
    rates = stage_labels(runs[0])

    def bands(ax, w):
        """Shade each stage and tag it with the rate it offered."""
        for n, s, e in w:
            ax.axvspan(s, e, color="0.90" if n % 2 == 0 else "0.83", zorder=0)
            if n in rates:
                ax.text((s + e) / 2, 0.97, rates[n], transform=ax.get_xaxis_transform(),
                        ha="center", va="top", fontsize=9, color="dimgray")

    def draw(ax, idx, xlim=None):
        for i in idx:
            d, c = data[i], COLORS[i % len(COLORS)]
            if len(d):
                ax.plot(d[:, 0], d[:, 1], "-", color=c, lw=1.8, label=labels[i])
        ax.set_xlabel("time since stage 0 start (s)")
        ax.set_ylabel(f"TTFT {name} (s), mc-a leaf")
        ax.grid(alpha=0.3)
        ax.legend(fontsize=9, loc="upper left", framealpha=0.95)
        if xlim:
            ax.set_xlim(*xlim)
        if a.log:
            ax.set_yscale("log")

    # Aggregate: all arms on one axis.
    fig, ax = plt.subplots(figsize=(13, 6))
    bands(ax, wins[0])
    draw(ax, range(len(data)))
    ax.set_title(f"TTFT {name} over the run — all arms (mc-a leaf, from vLLM histograms)")
    fig.tight_layout()
    out = os.path.join(a.outdir, f"{name}_ttft_timeseries_aggregate.png")
    fig.savefig(out, dpi=130)
    plt.close(fig)
    print("wrote", out)

    # One figure per arm.
    for i, label in enumerate(labels):
        fig, ax = plt.subplots(figsize=(12, 5))
        bands(ax, wins[i])
        draw(ax, [i])
        ax.set_title(f"TTFT {name} over the run — {label} (mc-a leaf)")
        fig.tight_layout()
        out = os.path.join(a.outdir, f"{name}_ttft_timeseries_{label}.png")
        fig.savefig(out, dpi=130)
        plt.close(fig)
        print("wrote", out)

    if a.per_stage:
        d = os.path.join(a.outdir, f"{name}_ttft_timeseries_stages")
        os.makedirs(d, exist_ok=True)
        # Stage n of every arm on one axis, rescaled to that rung.
        for n, s, e in wins[0]:
            fig, ax = plt.subplots(figsize=(11, 4.4))
            ax.axvspan(s, e, color="0.92", zorder=0)
            draw(ax, range(len(data)), xlim=(s - 30, e + 30))
            vis = [dd[(dd[:, 0] >= s - 30) & (dd[:, 0] <= e + 30), 1] for dd in data if len(dd)]
            top = max((v.max() for v in vis if len(v)), default=1)
            ax.set_ylim(0, top * 1.12)
            ax.set_title(f"stage {n} ({rates.get(n, '?')}) — TTFT {name}, all arms (mc-a leaf)")
            fig.tight_layout()
            tag = rates.get(n, str(n)).replace(" ", "")
            fig.savefig(os.path.join(d, f"stage{n:02d}_{tag}.png"), dpi=130)
            plt.close(fig)
        print(f"wrote {len(wins[0])} per-stage figures to {d}/")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Per-request e2e latency over the RPS ladder: scatter + binned p50.

One point per request at its arrival time, with a continuous median line across
every stage. Alternating grey bands mark the stages, labelled with the requested
rate so a latency knee can be read straight off the rung that caused it.

    plot_e2e_timeseries.py <results-dir> [--bin 15] [--log] [-o out.png]

<results-dir> is the directory `llmdbenchmark run` printed as `Local results:`,
or the run subdirectory inside it. Run extract_per_request_slim.py first --
per_request_lifecycle_metrics.json is multi-GB and is read once, not per plot.
"""
import argparse, datetime, glob, json, os, re, sys, tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "llmdbenchmark-matplotlib"))
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

STAGE_RE = re.compile(r"([0-9-]+ [0-9:,]+).*Stage ([0-9]+) - run (started|completed)")


def _wall(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def stage_windows(run_dir, cap=None):
    """[(stage, start, end)] as seconds elapsed from stage 0, parsed from harness stdout.

    Request times are a monotonic clock and these are wall clock, so both are
    reduced to elapsed-from-zero rather than joined directly.

    "run completed" is logged when the harness moves on, not when the last
    response lands, so a run that stalls afterwards reports an absurdly long
    final stage -- one baseline arm logged 14857s against 407s of requests,
    which would centre that band's label far off the right of the plot. Each
    stage therefore ends no later than the next begins, and the last no later
    than `cap`, the final request time.
    """
    st, en = {}, {}
    for line in open(f"{run_dir}/stdout.log", errors="replace"):
        m = STAGE_RE.search(line)
        if m:
            (st if m.group(3) == "started" else en)[int(m.group(2))] = _wall(m.group(1))
    if not st:
        return []
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


def stage_labels(run_dir):
    """{stage: label} naming what each stage held constant.

    Only open-loop stages report a rate; for `type: concurrent` inference-perf
    puts num_requests in requested_rate, which is not a rate at all. A closed-
    loop stage is the one that carries a `concurrency`.
    """
    out = {}
    for f in glob.glob(f"{run_dir}/stage_*_lifecycle_metrics.json"):
        n = int(f.split("stage_")[1].split("_")[0])
        s = json.load(open(f))["load_summary"]
        out[n] = (f"{s['concurrency']:g} concurrent" if s.get("concurrency")
                  else f"{s['requested_rate']:g} RPS")
    return out


def find_run_dir(base):
    if os.path.exists(f"{base}/stdout.log"):
        return base
    subs = [d for d in glob.glob(f"{base}/*") if os.path.exists(f"{d}/stdout.log")]
    if len(subs) != 1:
        sys.exit(f"expected one run dir under {base}, found {len(subs)}")
    return subs[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results")
    ap.add_argument("--bin", type=float, default=15.0, help="median bin width, seconds")
    ap.add_argument("--log", action="store_true", help="log y axis")
    ap.add_argument("--title", default=None)
    ap.add_argument("-o", "--output", default=None)
    a = ap.parse_args()

    run = find_run_dir(a.results)
    slim = f"{run}/per_request_slim.json"
    if not os.path.exists(slim):
        sys.exit(f"missing {slim}\nrun: extract_per_request_slim.py "
                 f"{run}/per_request_lifecycle_metrics.json {slim}")
    out = a.output or f"{run}/e2e_timeseries.png"

    recs = json.load(open(slim))
    t0 = min(r["t"] for r in recs)
    ok = np.array([(r["t"] - t0, r["lat"]) for r in recs if not r["fail"]])
    bad = np.array([(r["t"] - t0, r["lat"]) for r in recs if r["fail"]])

    fig, ax = plt.subplots(figsize=(13, 6))

    windows, labels = stage_windows(run, cap=ok[:, 0].max()), stage_labels(run)
    for n, s, e in windows:
        ax.axvspan(s, e, color="0.90" if n % 2 == 0 else "0.83", zorder=0)
        if n in labels:
            ax.text((s + e) / 2, 0.97, labels[n], transform=ax.get_xaxis_transform(),
                    ha="center", va="top", fontsize=9, color="dimgray")

    # Scale opacity to the point count: one setting cannot serve a 400-request
    # sim arm and an 11k-request GPU run.
    ax.scatter(ok[:, 0], ok[:, 1], s=3, color="#1f77b4", linewidths=0, zorder=2,
               label="request", alpha=min(0.6, max(0.08, 300 / len(ok))))
    if len(bad):
        ax.scatter(bad[:, 0], bad[:, 1], s=9, alpha=0.8, color="#d62728", marker="x",
                   zorder=4, label=f"failed ({len(bad)})")

    # Continuous p50 across the whole run -- bins do not reset at stage edges.
    b = (ok[:, 0] / a.bin).astype(int)
    ks = np.unique(b)
    p50 = np.array([np.median(ok[b == k, 1]) for k in ks])
    ax.plot((ks + 0.5) * a.bin, p50, "-", color="#ff7f0e", lw=2.0, zorder=3,
            label=f"p50 / {a.bin:g}s")

    ax.set_xlim(0, ok[:, 0].max() * 1.01)
    if a.log:
        ax.set_yscale("log")
    else:
        ax.set_ylim(0, np.percentile(ok[:, 1], 99.5) * 1.1)
    ax.set_xlabel("time since first request (s)")
    ax.set_ylabel("end-to-end latency (s)")
    ax.set_title(a.title or f"Per-request e2e latency over the RPS ladder ({os.path.basename(run)})")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=9, loc="upper left", framealpha=0.95)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"wrote {out}")
    print(f"  {len(ok)} ok, {len(bad)} failed; p50 peak {p50.max():.1f}s, final {p50[-1]:.1f}s")


if __name__ == "__main__":
    main()

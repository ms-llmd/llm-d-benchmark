#!/usr/bin/env python3
"""Predicted vs actual TTFT over time: every observation as a point, the
prediction as a line.

    plot_ttft_timeseries.py <epp.log> [--results <run-dir>] [--bin 15] [-o out.png]

Both series come from the router EPP's --v=4 log:

  ttft-aware score   ts, x-request-id, endpoint, predictedTTFT
  ttft-observation   x-request-id, endpoint, ttftSeconds

Each observation is placed at its request's **dispatch** instant -- the score
line's `ts`, joined on request id -- not at its own. The observation is logged
when the first chunk arrives, so plotting it at its own timestamp would shift
every point right by its own TTFT, which under load is tens of seconds and
smears the comparison exactly where it matters. (Same correction as
ipp_benchmarking/tools/plot_ttft_actual_vs_predicted.py.)

`--results` points at the harness run directory to shade and label the load
stages, and to set elapsed 0 at the harness's stage-0 start rather than at
whenever the log capture attached.
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


def find_run_dir(base):
    if not base:
        return None
    if os.path.exists(f"{base}/stdout.log"):
        return base
    subs = [d for d in glob.glob(f"{base}/*") if os.path.exists(f"{d}/stdout.log")]
    return subs[0] if len(subs) == 1 else None


def stage_windows(run_dir):
    """[(stage, start_epoch, end_epoch)] -- absolute, so they join to log ts."""
    st, en = {}, {}
    for line in open(f"{run_dir}/stdout.log", errors="replace"):
        m = STAGE_RE.search(line)
        if m:
            (st if m.group(3) == "started" else en)[int(m.group(2))] = _wall(m.group(1))
    ns = sorted(st)
    out = []
    for i, n in enumerate(ns):
        end = en.get(n, st[n])
        if i + 1 < len(ns):
            end = min(end, st[ns[i + 1]])
        out.append((n, st[n], max(end, st[n])))
    return out


def stage_labels(run_dir):
    """Only open-loop stages report a rate; a concurrent stage puts num_requests
    in requested_rate, so it is labelled by concurrency instead."""
    out = {}
    for f in glob.glob(f"{run_dir}/stage_*_lifecycle_metrics.json"):
        n = int(f.split("stage_")[1].split("_")[0])
        s = json.load(open(f))["load_summary"]
        out[n] = (f"{s['concurrency']:g} conc" if s.get("concurrency")
                  else f"{s['requested_rate']:g} RPS")
    return out


def load(path):
    """(dispatch_ts, actual, predicted, trusted) per joined request."""
    pred, obs = {}, {}
    for line in open(path, errors="replace"):
        if "ttft-aware score" not in line and "ttft-observation" not in line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        rid = d.get("x-request-id")
        if not rid:
            continue
        if d.get("msg") == "ttft-aware score":
            ep = d.get("endpoint", "")
            ep = ep.split("ID:", 1)[1].split(" ", 1)[0] if "ID:" in ep else ep
            pred[(rid, ep)] = (d.get("ts"), d.get("predictedTTFT"), bool(d.get("trusted")))
        elif d.get("msg") == "ttft-observation":
            obs[(rid, d.get("endpoint"))] = (d.get("ts"), d.get("ttftSeconds"))

    rows = []
    for key, (obs_ts, actual) in obs.items():
        if actual is None:
            continue
        if key in pred:
            ts, p, trusted = pred[key]
        else:
            # No score line (cold start before the scorer ran, or a lost line):
            # fall back to backing the TTFT out of the observation's own stamp.
            ts, p, trusted = (obs_ts or 0) - actual, np.nan, False
        rows.append((float(ts), float(actual), float(p) if p is not None else np.nan, trusted))
    if not rows:
        sys.exit(f"{path}: no joinable records -- was the EPP on the ttft arm at --v=4?")
    rows.sort()
    return np.array(rows, dtype=float)


def binned_median(x, y, bin_s):
    ok = ~np.isnan(y)
    if not ok.any():
        return np.array([]), np.array([])
    xs, ys = x[ok], y[ok]
    b = (xs / bin_s).astype(int)
    ks = np.unique(b)
    return (np.array([np.median(xs[b == k]) for k in ks]),
            np.array([np.median(ys[b == k]) for k in ks]))


def draw(ax, t, actual, predicted, bin_s):
    """Scatter of every observation plus the two median lines."""
    ax.scatter(t, actual, s=6, color="#2ca02c", linewidths=0, zorder=2,
               alpha=min(0.6, max(0.08, 300 / max(len(t), 1))),
               label="actual TTFT (per request)")
    at, av = binned_median(t, actual, bin_s)
    pt, pv = binned_median(t, predicted, bin_s)
    if len(at):
        ax.plot(at, av, "-", color="#1a7f1a", lw=2.0, zorder=3,
                label=f"actual median / {bin_s:g}s")
    if len(pt):
        ax.plot(pt, pv, "-", color="#9467bd", lw=2.4, zorder=4,
                label=f"predicted TTFT, median / {bin_s:g}s")
    ax.set_xlabel("time since stage 0 start (s)")
    ax.set_ylabel("TTFT (s)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8, loc="upper left", framealpha=0.95)


def per_stage(out, t, actual, predicted, windows, labels, t0, bin_s, pad=60.0):
    """One zoomed figure per stage, into <out-stem>_stages/.

    Each stage is rescaled to its own data. On the full-run figure a rung whose
    TTFT stays near zero is a flat line at the bottom of an axis stretched by a
    130s excursion elsewhere, so any error in it is invisible.
    """
    stem = os.path.splitext(out)[0]
    d = stem + "_stages"
    os.makedirs(d, exist_ok=True)
    wrote = 0
    for n, s, e in windows:
        x0, x1 = (s - t0) - pad, (e - t0) + pad
        m = (t >= x0) & (t <= x1)
        if not m.any():
            continue
        fig, ax = plt.subplots(figsize=(11, 4.4))
        draw(ax, t[m], actual[m], predicted[m], bin_s)
        ax.set_xlim(x0, x1)
        ax.set_ylim(0, np.nanmax(actual[m]) * 1.12 or 1)
        lbl = labels.get(n, f"stage {n}")
        ax.axvspan(s - t0, e - t0, color="0.92", zorder=0)
        err = predicted[m] - actual[m]
        err = err[~np.isnan(err)]
        note = (f"MAE {np.mean(np.abs(err)):.2f}s  bias {np.mean(err):+.2f}s"
                if len(err) else "no predictions")
        ax.set_title(f"stage {n} ({lbl}) — predicted vs actual TTFT   [{note}]")
        fig.tight_layout()
        fig.savefig(os.path.join(d, f"stage{n:02d}_{lbl.replace(' ', '')}.png"), dpi=130)
        plt.close(fig)
        print(f"  stage {n} ({lbl}): n={int(m.sum())}  {note}")
        wrote += 1
    print(f"wrote {wrote} per-stage figures to {d}/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("epp_log")
    ap.add_argument("--results", default=None, help="harness run dir, for stage bands")
    ap.add_argument("--bin", type=float, default=15.0, help="median window, seconds")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--per-stage", action="store_true",
                    help="also write one zoomed figure per stage (needs --results)")
    ap.add_argument("--title", default=None)
    ap.add_argument("-o", "--output", default="ttft_timeseries.png")
    a = ap.parse_args()

    rows = load(a.epp_log)
    run = find_run_dir(a.results)
    windows = stage_windows(run) if run else []
    # Elapsed 0 at the harness's stage-0 start, so bands line up with the run.
    t0 = windows[0][1] if windows else rows[0, 0]

    t = rows[:, 0] - t0
    actual, predicted = rows[:, 1], rows[:, 2]

    fig, ax = plt.subplots(figsize=(13, 6))

    for n, s, e in windows:
        ax.axvspan(s - t0, e - t0, color="0.90" if n % 2 == 0 else "0.83", zorder=0)
    if run:
        for n, lbl in stage_labels(run).items():
            w = [x for x in windows if x[0] == n]
            if w:
                ax.text((w[0][1] + w[0][2]) / 2 - t0, 0.97, lbl,
                        transform=ax.get_xaxis_transform(), ha="center", va="top",
                        fontsize=9, color="dimgray")

    ax.scatter(t, actual, s=6, color="#2ca02c", linewidths=0, zorder=2,
               alpha=min(0.6, max(0.08, 300 / len(t))), label="actual TTFT (per request)")

    at, av = binned_median(t, actual, a.bin)
    pt, pv = binned_median(t, predicted, a.bin)
    if len(at):
        ax.plot(at, av, "-", color="#1a7f1a", lw=2.0, zorder=3,
                label=f"actual median / {a.bin:g}s")
    if len(pt):
        ax.plot(pt, pv, "-", color="#9467bd", lw=2.4, zorder=4,
                label=f"predicted TTFT, median / {a.bin:g}s")

    n_pred = int((~np.isnan(predicted)).sum())
    print(f"{len(rows)} observations, {n_pred} with a prediction, "
          f"{len(rows) - n_pred} unmatched")
    if n_pred:
        ok = ~np.isnan(predicted)
        err = predicted[ok] - actual[ok]
        print(f"  MAE {np.mean(np.abs(err)):.3f}s   bias {np.mean(err):+.3f}s   "
              f"median actual {np.median(actual):.3f}s")

    if a.log:
        ax.set_yscale("log")
    else:
        ax.set_ylim(0, np.percentile(actual, 99.5) * 1.15)
    ax.set_xlim(0, t.max() * 1.01)
    ax.set_xlabel("time since stage 0 start (s)" if windows else "time since first request (s)")
    ax.set_ylabel("TTFT (s)")
    ax.set_title(a.title or f"Predicted vs actual TTFT ({os.path.basename(a.epp_log)})")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9, loc="upper left", framealpha=0.95)
    fig.tight_layout()
    fig.savefig(a.output, dpi=130)
    print("wrote", a.output)

    if a.per_stage:
        if not windows:
            sys.exit("--per-stage needs --results to know the stage windows")
        per_stage(a.output, t, actual, predicted, windows, stage_labels(run), t0, a.bin)


if __name__ == "__main__":
    main()

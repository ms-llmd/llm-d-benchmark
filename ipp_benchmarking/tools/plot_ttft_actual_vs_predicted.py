#!/usr/bin/env python3
"""Plot the queue-ttft scorer's PREDICTED effectiveTTFT against the ACTUAL
measured TTFT, from an IPP ipp-tail.log.

  plot_ttft_actual_vs_predicted.py <label>=<ipp-tail.log> [<label>=<log> ...] -o out.png

Predicted: "queue-ttft score" lines  -> effectiveTTFT (s), per request, ts.
Actual:    "ttft-observation" lines   -> ttft_s,        per request, ts.
The observation is emitted at END OF STREAM (PR #242 moved Notify there), so its
`ts` is the completion time -- we place each observation at its own request's
score `ts` (joined on x-request-id) to get the true dispatch instant.  Both then
share one x-axis and we compare the per-window medians.

Elapsed 0 is the harness's stage-0 start, so the stage bands line up with the
run rather than with whenever the log capture happened to attach.

Besides the full-run figure, writes one zoomed PNG per stage (the stage window
+ ~100s pad each side, dense time ticks) into `<out-stem>_stages/`.
"""
import datetime, gzip, json, os, re, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MultipleLocator

STAGE_PAD_S = 100.0  # seconds of context shown before/after each stage in zooms

BIN_S = 30.0  # time-window for median aggregation; override with --bin
UNIT = "C"  # band-label prefix; override with --unit (e.g. RPS for Poisson rate stages)
BANDS = True  # draw stage shading; --no-bands turns it off
# half-concurrency sweep for the static legs (half_8b/half_32b); override with --concurrencies
CONC_DEFAULT = [25, 75, 125, 175, 225, 275, 225, 175, 125, 75, 25]
_STAGE_RE = re.compile(r"([0-9-]+ [0-9:,]+).*Stage ([0-9]+) - run (started|completed)")


def stage_windows(stdout_path, conc):
    """Exact stage windows from the inference-perf harness log (UTC epoch, ground
    truth) mapped to each stage's profile concurrency. Returns [(conc, start, end)].
    Windows run start->completion, i.e. send phase AND the drain that follows; use
    send_windows() for the send phase alone."""
    starts, ends = {}, {}
    for line in open(stdout_path, errors="replace"):
        m = _STAGE_RE.search(line)
        if not m:
            continue
        ep = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S,%f").replace(
            tzinfo=datetime.timezone.utc).timestamp()
        (starts if m.group(3) == "started" else ends)[int(m.group(2))] = ep
    return [(conc[n], starts[n], ends[n]) for n in sorted(starts)
            if n in ends and n < len(conc)]


def run_t0(stdout_path, fallback):
    """Elapsed origin: the harness's stage-0 start. Falls back to the first
    logged request when there is no harness stdout beside the log."""
    if stdout_path:
        w = stage_windows(stdout_path, CONC_DEFAULT * 99)
        if w:
            return w[0][1]
    return fallback


def send_windows(wins, send_ts):
    """Each stage's SEND phase: start -> last request actually dispatched in it.
    What follows up to the stage's completion is drain (nothing dispatched)."""
    st = np.asarray(send_ts)
    return [(c, s, (st[(st >= s) & (st <= e)].max() if ((st >= s) & (st <= e)).any() else e))
            for c, s, e in wins]


def annotate_stages(ax, stdout_path, conc, t0, xmax, xmin=0, send_ts=None):
    """Grey = the stage's send phase, labelled with its rate. White = the drain
    that follows it, left unshaded and untagged. --no-bands skips both."""
    if not BANDS:
        return
    ytop = ax.get_ylim()[1]
    wins = stage_windows(stdout_path, conc)
    if send_ts is not None and len(send_ts):
        wins = send_windows(wins, send_ts)
    for c, s, e in wins:
        s0, e0 = s - t0, e - t0
        if e0 >= xmin and s0 <= xmax:  # stage (partially) visible
            lo, hi = max(s0, xmin), min(e0, xmax)
            ax.axvspan(lo, hi, color="0.90", zorder=0)
            ax.text((lo + hi) / 2, ytop * 0.97, f"{UNIT}={c:g}",
                    ha="center", va="top", fontsize=8, color="dimgray")


def plot_series(ax, at, av, pt, pv):
    if len(at):
        ax.plot(at, av, color="tab:green", lw=1.8, label=f"actual TTFT (median/{BIN_S:g}s)")
    if len(pt):
        ax.plot(pt, pv, color="tab:purple", lw=1.8, label=f"predicted effectiveTTFT (median/{BIN_S:g}s)")
    ax.set_xlabel("elapsed (s)"); ax.set_ylabel("TTFT (s)")
    ax.legend(fontsize=8, loc="center right"); ax.grid(alpha=0.3)


def per_stage_zooms(out, label, at, av, pt, pv, stdout_path, conc, t0, tag="", send_ts=None):
    """One zoomed PNG per stage: stage window +/- STAGE_PAD_S, dense time ticks."""
    stem, _ = os.path.splitext(out)
    d = stem + (f"_{tag}" if tag else "") + "_stages"; os.makedirs(d, exist_ok=True)
    wrote = 0
    for n, (c, s, e) in enumerate(stage_windows(stdout_path, conc)):
        x0, x1 = (s - t0) - STAGE_PAD_S, (e - t0) + STAGE_PAD_S
        win = [v[(t >= x0) & (t <= x1)] for t, v in ((at, av), (pt, pv)) if len(t)]
        if not any(len(w) for w in win):
            continue  # truncated log: no data in this stage's window
        ymax = max([np.nanmax(w) for w in win if len(w)] or [1])
        fig, ax = plt.subplots(figsize=(11, 4.2))
        plot_series(ax, at, av, pt, pv)
        ax.set_xlim(x0, x1); ax.set_ylim(0, ymax * 1.12)
        annotate_stages(ax, stdout_path, conc, t0, x1, xmin=x0, send_ts=send_ts)
        ax.xaxis.set_major_locator(MultipleLocator(20))
        ax.xaxis.set_minor_locator(MultipleLocator(5))
        ax.tick_params(axis="x", labelrotation=45)
        ax.set_title(f"{label}: stage {n} ({UNIT}={c:g}) predicted vs actual TTFT")
        fig.tight_layout(); fig.savefig(os.path.join(d, f"stage{n:02d}_{UNIT}{c:g}.png"), dpi=130)
        plt.close(fig); wrote += 1
    print(f"{label}: wrote {wrote} stage zooms to {d}/")


SHORT = {"Qwen/Qwen3-8B": "8B", "Qwen/Qwen3-32B": "32B"}


def parse(path, split_model=False):
    """{key: (pred, act)} of (ts, value) arrays, both stamped at the request's
    dispatch instant (the score line's ts, joined on x-request-id). key is the
    model name when split_model, else "" (pooled)."""
    from collections import defaultdict
    pred, act = defaultdict(list), defaultdict(list)
    scored, seen = {}, set()   # (rid, model) -> (dispatch ts, predicted)
    with (gzip.open if path.endswith(".gz") else open)(path, "rt", errors="replace") as f:
        for line in f:
            if "queue-ttft score" not in line and "ttft-aware score" not in line and "ttft-observation" not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            k = r.get("model", "") if split_model else ""
            m, rid, mdl = r.get("msg"), r.get("x-request-id"), r.get("model")
            if (rid, r["ts"], mdl) in seen:
                continue  # duplicate capture (two log streamers appending to one file)
            seen.add((rid, r["ts"], mdl))
            if m in ("queue-ttft score", "ttft-aware score") and ("effectiveTTFT" in r or "predictedTTFT" in r):
                v = r.get("effectiveTTFT", r.get("predictedTTFT"))
                scored.setdefault((rid, mdl), r["ts"])   # per candidate model
                pred[k].append((r["ts"], v))
            elif m == "ttft-observation" and "ttft_s" in r:
                # observation is logged at end-of-stream; place it at its own
                # request's score ts, else ts-ttft_s leaves the decode duration in.
                act[k].append((scored.get((rid, mdl), r["ts"] - r["ttft_s"]), r["ttft_s"]))
    return {k: (np.array(pred.get(k, [])), np.array(act.get(k, [])))
            for k in set(pred) | set(act)}


def binned_median(ts, val, t0, bin_s=None, stages=None):
    """Median per time window, in elapsed seconds.

    Bins restart at every stage boundary so none straddles two stages, and each
    point sits at the MEDIAN DISPATCH TIME of its own bin rather than a nominal
    edge/centre -- with a partly-filled bin (every stage's last one) an edge label
    is off by up to half a bin.  The series stays connected across stages; note a
    saturated stage sends for ~290s then drains for ~160s with nothing dispatched,
    so the segment spanning a boundary is interpolation, not measurement."""
    bin_s = BIN_S if bin_s is None else bin_s  # read at call time, so --bin applies
    if len(ts) == 0:
        return np.array([]), np.array([])
    e = ts - t0
    out_t, out_v = [], []
    for s, end in (stages or [(e.min(), e.max() + bin_s)]):
        m = (e >= s) & (e < end)
        if not m.any():
            continue
        ee, vv = e[m], val[m]
        b = ((ee - s) // bin_s).astype(int)
        for k in np.unique(b):
            q = b == k
            out_t.append(np.median(ee[q]))
            out_v.append(np.median(vv[q]))
    return np.array(out_t), np.array(out_v)


def main():
    args = sys.argv[1:]
    out = "ttft_actual_vs_predicted.png"
    conc = CONC_DEFAULT
    if "-o" in args:
        i = args.index("-o"); out = args[i + 1]; args = args[:i] + args[i + 2:]
    if "--concurrencies" in args:
        i = args.index("--concurrencies")
        conc = [float(c) for c in args[i + 1].split(",")]; args = args[:i] + args[i + 2:]
    if "--unit" in args:
        i = args.index("--unit")
        global UNIT; UNIT = args[i + 1]; args = args[:i] + args[i + 2:]
    if "--bin" in args:
        i = args.index("--bin")
        global BIN_S; BIN_S = float(args[i + 1]); args = args[:i] + args[i + 2:]
    if "--no-bands" in args:
        args.remove("--no-bands"); global BANDS; BANDS = False
    by_model = False
    if "--by-model" in args:
        args.remove("--by-model"); by_model = True
    runs = [a.split("=", 1) for a in args]

    # rows: (label, path, tag, pred, act). --by-model splits one log per model.
    rows = []
    for label, path in runs:
        for key, (pred, act) in sorted(parse(path, split_model=by_model).items()):
            tag = SHORT.get(key, key)
            rows.append((f"{label} {tag}" if tag else label, path, tag, pred, act))

    n = len(rows)
    fig, axes = plt.subplots(n, 1, figsize=(11, 4.2 * n), squeeze=False)
    for row, (label, path, tag, pred, act) in enumerate(rows):
        ax = axes[row][0]
        if len(pred) == 0 and len(act) == 0:
            ax.set_title(f"{label}: no data"); continue
        d = os.path.dirname(path)  # log may sit in <arm>/ or <arm>/oc-logs/
        stdout_path = next((c for c in (os.path.join(d, "harness_stdout.log"),
                            os.path.join(os.path.dirname(d), "harness_stdout.log"))
                            if os.path.exists(c)), None)
        first = min([x[0][0] for x in (pred, act) if len(x)])
        t0 = run_t0(stdout_path, first)
        wins = [(s - t0, e - t0) for _, s, e in stage_windows(stdout_path, conc)] if stdout_path else None

        pt, pv = binned_median(pred[:, 0], pred[:, 1], t0, stages=wins) if len(pred) else (np.array([]), np.array([]))
        at, av = binned_median(act[:, 0], act[:, 1], t0, stages=wins) if len(act) else (np.array([]), np.array([]))

        plot_series(ax, at, av, pt, pv)
        ax.set_title(f"{label}: predicted vs actual TTFT over time")

        ok = lambda v: len(v) and not np.all(np.isnan(v))  # series carry NaN stage breaks
        xmax = max([np.nanmax(v) for v in (at, pt) if ok(v)] or [0])
        ymax = max([np.nanmax(v) for v in (av, pv) if ok(v)] or [1])
        if stdout_path:
            w = stage_windows(stdout_path, conc)
            if w:
                xmax = max(xmax, w[-1][2] - t0)
        ax.set_xlim(0, xmax); ax.set_ylim(0, ymax * 1.12)
        if stdout_path:
            stages = stage_windows(stdout_path, conc)
            if stages and not any(s - t0 <= xmax and e - t0 >= 0 for _, s, e in stages):
                print(f"WARNING: {label}: {stdout_path} stages don't overlap the IPP "
                      f"log's time span -- mismatched runs? no stage bands/zooms.", file=sys.stderr)
            send_ts = pred[:, 0] if len(pred) else act[:, 0]  # dispatch epochs -> grey ends at last sent
            annotate_stages(ax, stdout_path, conc, t0, xmax, send_ts=send_ts)
            per_stage_zooms(out, label, at, av, pt, pv, stdout_path, conc, t0, tag=tag, send_ts=send_ts)
        print(f"{label}: predicted={len(pred)} actual={len(act)} bins={len(pt)} "
              f"t0={'harness stage-0' if stdout_path else 'first request'} "
              f"(first request at {first - t0:+.1f}s)")

    fig.tight_layout(); fig.savefig(out, dpi=130)
    print("wrote", out)


if __name__ == "__main__":
    main()

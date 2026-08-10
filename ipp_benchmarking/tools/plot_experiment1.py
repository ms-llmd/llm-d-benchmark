#!/usr/bin/env python3
"""
plot_experiment1.py — Analysis plots for Experiment 1 (auto model selection).

Compares MULTIPLE ARMS (scorer vs baselines) — this is the point of the experiment.

Expected layout:
    <run-dir>/
        <arm-subdir>/                 # one per arm, e.g. scorer/, 8b-only/, static-70-30/
            per_request_slim.json
            harness_stdout.log
        ipp-full-live.log             # IPP log for the scorer arm (optional)

Model keys in per_request_slim.json:  "small" → Qwen3-8B,  "big" → Qwen3-32B

Usage:
    # single arm (accuracy/mechanism plots only)
    python plot_experiment1.py --run-dir run-experiment1/ --arm scorer=scorer/

    # full comparison (the blog figures)
    python plot_experiment1.py --run-dir run-experiment1/ \
        --arm scorer=scorer/ --arm 8b-only=8b-only/ --arm static-70-30=static/ \
        --scorer-arm scorer

Outputs (to --out-dir, default <run-dir>/plots/):
    exp1_traffic_share.png        [scorer arm]  request share per model per stage
    exp1_ttft_timeseries.png      [scorer arm]  TTFT over time per model  (log-y)
    exp1_latency_vs_load.png      [ALL arms]    p50/p95 TTFT vs RPS       (log-y)  <-- headline
    exp1_completions.png          [ALL arms]    completed vs expected requests per stage
    exp1_predicted_vs_actual.png  [scorer arm]  predicted vs actual TTFT  (log-log)
"""

import argparse
import bisect
import datetime
import json
import re
import sys
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# ── Palette / labels ───────────────────────────────────────────────────────────
COLORS = {"small": "#2196F3", "big": "#FF9800", "combined": "#4CAF50"}
LABELS = {"small": "Qwen3-8B", "big": "Qwen3-32B", "combined": "Combined"}
# stable per-arm colors (assigned in order given on the CLI)
ARM_COLORS = ["#7B2D8E", "#2E8B57", "#E08214", "#888888", "#1f77b4", "#C0504D"]

IPP_MODEL_MAP = {"Qwen/Qwen3-8B": "small", "Qwen/Qwen3-32B": "big"}

FIGSIZE_WIDE = (13, 5)
FIGSIZE_SQ   = (7, 6)
SCATTER_S, SCATTER_A = 5, 0.25
ROLLING_WIN = 60
# FLOOR for log axes: TTFT can't be 0; clip so log plots don't drop points.
LOG_FLOOR = 1e-3


# ── Parsing ────────────────────────────────────────────────────────────────────

def parse_harness_log(log_path: Path):
    text = log_path.read_text(encoding="utf-8", errors="replace")
    rates = [float(r) for r in re.findall(r"rate:\s+([\d.]+)", text)]

    starts, ends = {}, {}
    ts_fmt = "%Y-%m-%d %H:%M:%S"
    for m in re.finditer(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+.*?Stage (\d+) - run (started|completed)",
        text,
    ):
        ts_str, stage_idx, event = m.group(1), int(m.group(2)), m.group(3)
        dt = datetime.datetime.strptime(ts_str, ts_fmt).replace(tzinfo=datetime.timezone.utc)
        (starts if event == "started" else ends)[stage_idx] = dt.timestamp()

    if not starts:
        raise RuntimeError(f"No stage start lines parsed from {log_path}")
    return rates, starts, ends


def load_requests(json_path: Path):
    with open(json_path) as f:
        return json.load(f)


def build_elapsed(records, stage_starts):
    """
    Elapsed seconds from experiment start, consistent across request records and stage
    boundaries.

    Request records use the process monotonic clock (`t`).  Harness log timestamps are
    wall-clock (epoch).  These clocks share no common zero, so we anchor each on its
    own zero:
      - requests: elapsed = t - t_min  (first request = 0)
      - stages:   elapsed = epoch_ts - epoch_t0  (stage 0 start = 0)

    Both produce the same ~0-3437s range relative to experiment start.  The two zeros
    differ by at most the harness startup time before the first request fires (<1s),
    which is negligible for stage-level analysis.

    epoch_t0 is returned as-is for IPP log alignment (IPP uses epoch timestamps too).
    """
    t_vals = np.asarray([r["t"] for r in records], dtype=float)
    t_min = float(t_vals.min())
    base = min(stage_starts.keys())
    epoch_t0 = stage_starts[base]

    # elapsed relative to first request (monotonic ref)
    elapsed = t_vals - t_min
    # stage boundaries relative to stage-0 start (epoch ref) — same scale as elapsed
    stage_elap = {i: s - epoch_t0 for i, s in stage_starts.items()}
    return elapsed, epoch_t0, stage_elap


def compute_stage_stats(records, elapsed, stage_elap, stage_ends, epoch_t0, rates):
    """Per-stage stats. Guards a missing final 'completed' line."""
    max_elapsed = float(elapsed.max())
    stats = []
    keys = sorted(stage_elap.keys())
    for n, i in enumerate(keys):
        if i - keys[0] >= len(rates):
            continue
        rate = rates[i - keys[0]]
        s_e = stage_elap[i]
        if i in stage_ends:
            e_e = stage_ends[i] - epoch_t0
        elif n + 1 < len(keys):
            e_e = stage_elap[keys[n + 1]]      # next stage start
        else:
            e_e = max_elapsed                   # FIX: was t0+99999 → axis blowup
        mask = (elapsed >= s_e) & (elapsed < e_e)
        recs_s = [r for r, m in zip(records, mask) if m]
        by_model = {}
        for rec in recs_s:
            by_model.setdefault(rec["m"], []).append(rec["lat"])
        stats.append(dict(
            stage=i, rate=rate, count=len(recs_s),
            by_model={k: np.asarray(v) for k, v in by_model.items()},
            all_lats=np.asarray([r["lat"] for r in recs_s]),
            s_e=s_e, e_e=e_e, duration=max(e_e - s_e, 1e-9),
        ))
    return stats


def load_ipp_scores(ipp_log: Path, epoch_t0: float, max_elapsed: float):
    """Winning-decision rows from the IPP log, elapsed anchored on stage-0 start."""
    rows = []
    with open(ipp_log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"ttft-aware score"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            try:
                el = rec["ts"] - epoch_t0
                if el < 0 or el > max_elapsed + 60:
                    continue
                mkey = IPP_MODEL_MAP.get(rec["model"])
                if mkey is None:
                    continue
                rows.append(dict(
                    elapsed=el,
                    model_key=mkey,
                    effectiveTTFT=rec["effectiveTTFT"],
                    score=rec["score"],
                    trusted=rec.get("trusted"),
                    # request id enables EXACT matching; may be absent in older logs
                    rid=rec.get("x-request-id") or rec.get("request_id"),
                ))
            except KeyError:
                continue
    return rows


# ── Helpers ────────────────────────────────────────────────────────────────────

def rolling_percentile(ts, vals, window_s, pct, min_n=10, step=25):
    """O(n log n) rolling percentile (old version was O(n^2))."""
    idx = np.argsort(ts)
    ts_s, vals_s = np.asarray(ts)[idx], np.asarray(vals)[idx]
    centers, out = [], []
    half = window_s / 2.0
    ts_list = ts_s.tolist()
    for ci in range(0, len(ts_s), step):
        c = ts_s[ci]
        lo = bisect.bisect_left(ts_list, c - half)
        hi = bisect.bisect_right(ts_list, c + half)
        if hi - lo >= min_n:
            centers.append(c)
            out.append(float(np.percentile(vals_s[lo:hi], pct)))
    return np.asarray(centers), np.asarray(out)


def add_stage_bands(ax, stage_stats):
    for i, s in enumerate(stage_stats):
        ax.axvspan(s["s_e"], s["e_e"], color=("0.85" if i % 2 == 0 else "0.92"), zorder=0)
        ax.axvline(s["s_e"], color="0.6", lw=0.5, zorder=1)


def annotate_stage_rps(ax, stage_stats):
    """Log-safe stage annotation (old version linearly interpolated on a log axis)."""
    y0, y1 = ax.get_ylim()
    y = (10 ** (np.log10(y0) + 0.95 * (np.log10(y1) - np.log10(y0)))
         if ax.get_yscale() == "log" else y0 + 0.95 * (y1 - y0))
    for s in stage_stats:
        ax.text((s["s_e"] + s["e_e"]) / 2, y, f"{s['rate']:g}", ha="center", va="top",
                fontsize=6, color="0.4", zorder=5)


def safe_log(v):
    return np.clip(np.asarray(v, dtype=float), LOG_FLOOR, None)


# ── Arm loading ────────────────────────────────────────────────────────────────

def load_arm(name, path: Path):
    harness = path / "harness_stdout.log"
    slim    = path / "per_request_slim.json"
    if not slim.exists():
        raise FileNotFoundError(f"[{name}] missing {slim}")
    rates, starts, ends = parse_harness_log(harness)
    records = load_requests(slim)
    elapsed, epoch_t0, stage_elap = build_elapsed(records, starts)
    stats = compute_stage_stats(records, elapsed, stage_elap, ends, epoch_t0, rates)
    print(f"  [{name}] {len(records)} requests, {len(stats)} stages, "
          f"models={sorted({r['m'] for r in records})}")
    return dict(name=name, path=path, rates=rates, records=records,
                elapsed=elapsed, epoch_t0=epoch_t0, stage_stats=stats)


# ── Plot: traffic share (scorer arm) ───────────────────────────────────────────

BACKGROUND_COLOR = "#C0504D"   # same red used elsewhere for "32B background active"


def _background_counts_per_stage(arm, background, stage_stats):
    """
    Count background-arm requests falling into each of `arm`'s stage windows.

    `background` is an independent process (its own harness run, own monotonic
    clock) so its request timestamps ("t") are not comparable to `arm`'s. Both
    arms' harness logs carry wall-clock stage-start timestamps though, so we
    anchor each on its own epoch_t0 (see build_elapsed) and shift the
    background's elapsed-since-its-own-start onto arm's elapsed-since-its-own-
    start by the difference between the two wall-clock epochs.
    """
    shift = background["epoch_t0"] - arm["epoch_t0"]
    bg_elapsed = background["elapsed"] + shift
    counts = np.zeros(len(stage_stats))
    for i, s in enumerate(stage_stats):
        counts[i] = int(np.sum((bg_elapsed >= s["s_e"]) & (bg_elapsed < s["e_e"])))
    return counts


def plot_traffic_share(arm, out_path: Path, background=None,
                        background_label="Background (32B)"):
    ss = arm["stage_stats"]
    models = sorted({k for s in ss for k in s["by_model"]})
    x = np.arange(len(ss))

    bg_counts = (_background_counts_per_stage(arm, background, ss)
                 if background is not None else np.zeros(len(ss)))
    totals = np.array([s["count"] for s in ss], dtype=float) + bg_counts

    fig, ax = plt.subplots(figsize=(10, 5))
    bottoms = np.zeros(len(ss))
    for mkey in models:
        counts = np.array([len(s["by_model"].get(mkey, [])) for s in ss], dtype=float)
        shares = np.divide(100 * counts, totals, out=np.zeros_like(counts), where=totals > 0)
        ax.bar(x, shares, 0.6, bottom=bottoms, label=LABELS.get(mkey, mkey),
               color=COLORS.get(mkey, "#888"), alpha=0.85, zorder=3)
        for xi, (sh, bo) in enumerate(zip(shares, bottoms)):
            if sh > 5:
                ax.text(xi, bo + sh / 2, f"{sh:.0f}%", ha="center", va="center",
                        fontsize=7, color="white", fontweight="bold")
        bottoms += shares

    if background is not None:
        bg_shares = np.divide(100 * bg_counts, totals, out=np.zeros_like(bg_counts),
                              where=totals > 0)
        ax.bar(x, bg_shares, 0.6, bottom=bottoms, label=background_label,
               color=BACKGROUND_COLOR, alpha=0.85, hatch="//", zorder=3)
        for xi, (sh, bo) in enumerate(zip(bg_shares, bottoms)):
            if sh > 5:
                ax.text(xi, bo + sh / 2, f"{sh:.0f}%", ha="center", va="center",
                        fontsize=7, color="white", fontweight="bold")
        bottoms += bg_shares

    ax.axhline(50, color="k", ls="--", lw=1.2, alpha=0.6, label="50% reference")
    ax.set_xticks(x); ax.set_xticklabels([f"{s['rate']:g}" for s in ss], fontsize=8)
    ax.set_xlabel("RPS"); ax.set_ylabel("Request share (%)"); ax.set_ylim(0, 100)
    subtitle = ("(scorer shifts load to 32B as 8B saturates; hatched = constant "
                "background load on 32B)" if background is not None else
                "(scorer shifts load to 32B as 8B saturates)")
    ax.set_title(f"Traffic distribution per stage — {arm['name']}\n{subtitle}", fontsize=10)
    ax.legend(fontsize=8, loc="upper right"); ax.grid(axis="y", alpha=0.3, zorder=0)
    fig.tight_layout(); fig.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: TTFT timeseries (scorer arm), LOG-Y ─────────────────────────────────

def plot_ttft_timeseries(arm, out_path: Path):
    records, elapsed, ss = arm["records"], arm["elapsed"], arm["stage_stats"]
    fig, ax = plt.subplots(figsize=FIGSIZE_WIDE)
    add_stage_bands(ax, ss)
    for mkey in sorted({r["m"] for r in records}):
        mask = np.array([r["m"] == mkey for r in records])
        ts_m = elapsed[mask]
        lat_m = safe_log(np.array([r["lat"] for r in records])[mask])
        c = COLORS.get(mkey, "#888"); lab = LABELS.get(mkey, mkey)
        ax.scatter(ts_m, lat_m, c=c, s=SCATTER_S, alpha=SCATTER_A, linewidths=0,
                   label=f"{lab} (raw)", rasterized=True, zorder=3)
        cx, cy = rolling_percentile(ts_m, lat_m, ROLLING_WIN, 50)
        if len(cx):
            ax.plot(cx, cy, color=c, lw=2, label=f"{lab} (rolling p50)", zorder=4)
    ax.set_yscale("log")            # FIX: was linear; TTFT spans orders of magnitude
    annotate_stage_rps(ax, ss)
    ax.set_xlabel("Elapsed time (s)"); ax.set_ylabel("TTFT (s, log)")
    ax.set_title(f"TTFT over time — {arm['name']} (Qwen3-8B + Qwen3-32B)", fontsize=10)
    ax.legend(loc="upper left", fontsize=7, markerscale=3, ncol=2)
    ax.grid(axis="y", alpha=0.3, which="both", zorder=0)
    fig.tight_layout(); fig.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: latency vs load, ALL ARMS (the headline comparison) ─────────────────

def plot_latency_vs_load(arms, out_path: Path):
    fig, axes = plt.subplots(1, 2, figsize=FIGSIZE_WIDE)
    for ax, pct in zip(axes, [50, 95]):
        for ai, arm in enumerate(arms):
            ss = arm["stage_stats"]
            rates = [s["rate"] for s in ss]
            vals = [float(np.percentile(s["all_lats"], pct)) if len(s["all_lats"]) else np.nan
                    for s in ss]
            ax.plot(rates, safe_log(vals), marker="o", lw=2, ms=5,
                    color=ARM_COLORS[ai % len(ARM_COLORS)], label=arm["name"], zorder=3)
        ax.set_yscale("log")
        ax.set_xlabel("Offered load (RPS)")
        ax.set_ylabel(f"TTFT p{pct} (s, log)")
        ax.set_title(f"P{pct} TTFT vs load — all arms", fontsize=10)
        ax.legend(fontsize=8); ax.grid(alpha=0.3, which="both")
    fig.suptitle("Experiment 1 — combined TTFT by arm (lower is better)", fontsize=11, y=1.02)
    fig.tight_layout(); fig.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: completions (survivorship-bias guard) ───────────────────────────────

def plot_completions(arms, out_path: Path):
    """
    Latency percentiles only cover COMPLETED requests. If a saturated arm drops or times
    out requests, its percentiles look artificially good. Always show completion counts
    next to the latency plot.
    """
    fig, ax = plt.subplots(figsize=(10, 4.5))
    n = len(arms)
    width = 0.8 / n
    for ai, arm in enumerate(arms):
        ss = arm["stage_stats"]
        x = np.arange(len(ss))
        completed = np.array([s["count"] for s in ss], dtype=float)
        expected = np.array([s["rate"] * s["duration"] for s in ss], dtype=float)
        ratio = 100 * np.divide(completed, expected, out=np.zeros_like(completed),
                                where=expected > 0)
        ax.bar(x + ai * width - 0.4 + width / 2, ratio, width,
               color=ARM_COLORS[ai % len(ARM_COLORS)], label=arm["name"], alpha=0.85, zorder=3)
    ax.axhline(100, color="k", ls="--", lw=1, alpha=0.6)
    ss0 = arms[0]["stage_stats"]
    ax.set_xticks(np.arange(len(ss0)))
    ax.set_xticklabels([f"{s['rate']:g}" for s in ss0], fontsize=8)
    ax.set_xlabel("RPS"); ax.set_ylabel("Completed / expected (%)")
    ax.set_title("Completion rate per stage — drops/timeouts would bias the latency "
                 "percentiles above", fontsize=10)
    ax.legend(fontsize=8); ax.grid(axis="y", alpha=0.3, zorder=0)
    fig.tight_layout(); fig.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: predicted vs actual (scorer arm), LOG-LOG ───────────────────────────

def plot_predicted_vs_actual(arm, ipp_scores, out_path: Path):
    records, elapsed = arm["records"], arm["elapsed"]
    model_keys = sorted({r["m"] for r in records})

    # Prefer EXACT request-id matching; fall back to nearest-time only if unavailable.
    have_rid = any(s.get("rid") for s in ipp_scores) and any("id" in r for r in records[:50])
    matched = {m: {"pred": [], "actual": []} for m in model_keys}

    if have_rid:
        lat_by_rid = {r["id"]: (r["m"], r["lat"]) for r in records if "id" in r}
        for d in ipp_scores:
            if d["score"] != 1 or not d.get("rid"):
                continue
            hit = lat_by_rid.get(d["rid"])
            if hit and hit[0] == d["model_key"]:
                matched[d["model_key"]]["pred"].append(d["effectiveTTFT"])
                matched[d["model_key"]]["actual"].append(hit[1])
        print("  matching: exact by request-id")
    else:
        print("  WARNING: no request-id in IPP log/records — falling back to nearest-time "
              "matching (±0.5s). At high RPS this can mis-pair requests; prefer logging "
              "x-request-id on both sides.")
        req_by_model = {}
        for m in model_keys:
            mask = np.array([r["m"] == m for r in records])
            te = elapsed[mask]
            la = np.array([r["lat"] for r in records])[mask]
            si = np.argsort(te)
            req_by_model[m] = (te[si], la[si], te[si].tolist())
        for d in ipp_scores:
            if d["score"] != 1:
                continue
            m = d["model_key"]
            if m not in req_by_model:
                continue
            te, la, te_list = req_by_model[m]
            j = bisect.bisect_left(te_list, d["elapsed"])
            best, bd = None, 1e9
            for k in (j - 1, j, j + 1):
                if 0 <= k < len(te):
                    dd = abs(te[k] - d["elapsed"])
                    if dd < bd:
                        best, bd = k, dd
            if best is not None and bd <= 0.5:     # tightened from 2.0s
                matched[m]["pred"].append(d["effectiveTTFT"])
                matched[m]["actual"].append(la[best])

    fig, ax = plt.subplots(figsize=FIGSIZE_SQ)
    has = False
    for m in model_keys:
        p, a = np.array(matched[m]["pred"]), np.array(matched[m]["actual"])
        if len(p) == 0:
            continue
        has = True
        ax.scatter(safe_log(a), safe_log(p), c=COLORS.get(m, "#888"), s=SCATTER_S + 2,
                   alpha=SCATTER_A + 0.1, linewidths=0,
                   label=f"{LABELS.get(m, m)} (n={len(p)})", rasterized=True, zorder=3)
    if not has:
        ax.text(0.5, 0.5, "No matched data", transform=ax.transAxes, ha="center", va="center")
        fig.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
        print(f"  Saved {out_path} (no data)"); return

    lo = min(ax.get_xlim()[0], ax.get_ylim()[0], LOG_FLOOR * 10)
    hi = max(ax.get_xlim()[1], ax.get_ylim()[1])
    ax.plot([lo, hi], [lo, hi], "k--", lw=1, alpha=0.6, label="y = x (perfect)", zorder=2)
    ax.set_xscale("log"); ax.set_yscale("log")     # FIX: was linear/linear
    ax.set_xlim(lo, hi); ax.set_ylim(lo, hi)
    # NOTE axes swapped vs original: convention is actual on X, predicted on Y.
    ax.set_xlabel("Actual TTFT (s, log)"); ax.set_ylabel("Predicted effectiveTTFT (s, log)")
    ax.set_title(f"Prediction vs actual TTFT — {arm['name']}\n"
                 "points below y=x → under-prediction", fontsize=10)
    ax.legend(fontsize=8, markerscale=3); ax.grid(alpha=0.3, which="both")
    fig.tight_layout(); fig.savefig(out_path, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: 8B latency timeseries comparison (with 32B-active markers) ──────────

def _find_active_periods(timestamps, bin_s=30.0):
    """Return list of (start, end) contiguous bins where at least one timestamp falls."""
    if len(timestamps) == 0:
        return []
    ts = np.sort(timestamps)
    lo = (ts[0] // bin_s) * bin_s
    hi = lo + bin_s
    periods = []
    for t in ts:
        b = (t // bin_s) * bin_s
        if b <= hi:
            hi = b + bin_s
        else:
            periods.append((lo, hi))
            lo, hi = b, b + bin_s
    periods.append((lo, hi))
    return periods


def plot_8b_latency_timeseries(arms, out_path: Path, model_key="small",
                                offload_arm_name=None):
    """
    Rolling-median timeseries of 8B-only e2e latency for each arm.
    Red strips at the bottom mark periods when the offload arm was routing to 32B.
    """
    fig, ax = plt.subplots(figsize=FIGSIZE_WIDE)
    ss0 = arms[0]["stage_stats"]
    add_stage_bands(ax, ss0)

    offload_arm = next((a for a in arms if a["name"] == offload_arm_name), None) \
        if offload_arm_name else None

    for ai, arm in enumerate(arms):
        records, elapsed = arm["records"], arm["elapsed"]
        mask = np.array([r["m"] == model_key for r in records])
        ts_m = elapsed[mask]
        lat_m = safe_log(np.array([r["lat"] for r in records])[mask])
        if len(ts_m) == 0:
            print(f"  WARNING: arm {arm['name']!r} has no {model_key!r} records — skipping")
            continue
        color = ARM_COLORS[ai % len(ARM_COLORS)]
        cx, cy = rolling_percentile(ts_m, lat_m, ROLLING_WIN, 50)
        if len(cx):
            ax.plot(cx, cy, color=color, lw=2, label=arm["name"], zorder=4)

    ax.set_yscale("log")
    y_lo, y_hi = ax.get_ylim()

    if offload_arm is not None:
        big_ts = offload_arm["elapsed"][
            np.array([r["m"] != model_key for r in offload_arm["records"]])
        ]
        periods = _find_active_periods(big_ts)
        if periods:
            # Thin strip occupying the bottom 4% of the log range
            strip_top = 10 ** (np.log10(y_lo) + 0.04 * (np.log10(y_hi) - np.log10(y_lo)))
            for lo_p, hi_p in periods:
                ax.fill_between([lo_p, hi_p], y_lo, strip_top,
                                color="#C0504D", alpha=0.75, zorder=5, linewidth=0)
            ax.fill_between([], [], [],
                            color="#C0504D", alpha=0.75,
                            label=f"32B background active ({offload_arm_name})")

    ax.set_ylim(y_lo, y_hi)
    annotate_stage_rps(ax, ss0)
    ax.set_xlabel("elapsed time since 8B Stage-0 (s)  [top labels = 8B stage RPS]")
    ax.set_ylabel("8B e2e latency (s, log)")
    ax.set_title("8B e2e latency over time (median/{}s): {}".format(
        ROLLING_WIN, " vs ".join(a["name"] for a in arms)), fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, which="both", zorder=0)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: 8B per-stage p50 pyramid comparison ─────────────────────────────────

def plot_8b_per_stage_pyramid(arms, out_path: Path, model_key="small"):
    """
    Per-stage p50 of 8B-only e2e latency for each arm.
    X-axis follows the pyramid RPS pattern (ramp-up then ramp-down).
    """
    fig, ax = plt.subplots(figsize=(10, 5))

    for ai, arm in enumerate(arms):
        ss = arm["stage_stats"]
        x = list(range(len(ss)))
        vals = []
        for s in ss:
            m_lats = s["by_model"].get(model_key, np.array([]))
            vals.append(float(np.percentile(m_lats, 50)) if len(m_lats) > 0 else np.nan)
        color = ARM_COLORS[ai % len(ARM_COLORS)]
        ax.plot(x, safe_log(np.array(vals, dtype=float)),
                marker="o", lw=2, ms=6, color=color,
                label=f"{arm['name']} p50", zorder=3)

    ax.set_yscale("log")
    ss0 = arms[0]["stage_stats"]
    ax.set_xticks(range(len(ss0)))
    ax.set_xticklabels([f"{s['rate']:g}" for s in ss0])
    ax.set_xlabel("stage requested RPS")
    ax.set_ylabel("end-to-end latency (s, log)")
    ax.set_title("8B end-to-end latency per stage (p50): pinned vs auto-offload (pyramid)",
                 fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out_path}")


# ── IPP TTFT observation loading ──────────────────────────────────────────────

def find_ipp_log(arm_path: Path):
    """Return the IPP log path for an arm (arm dir or its parent), or None."""
    for candidate in [arm_path / "ipp-full-live.log", arm_path.parent / "ipp-full-live.log"]:
        if candidate.exists():
            return candidate
    return None


def load_ipp_ttft_obs(ipp_log: Path, epoch_t0: float, model_key: str = "small"):
    """
    Parse ttft-observation lines from an IPP log.
    Returns (elapsed_array, ttft_s_array) anchored on epoch_t0 (stage-0 start).
    """
    elapsed_list, ttft_list = [], []
    with open(ipp_log, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "ttft-observation" not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if IPP_MODEL_MAP.get(rec.get("model")) != model_key:
                continue
            elapsed_list.append(rec["ts"] - epoch_t0)
            ttft_list.append(rec["ttft_s"])
    return np.array(elapsed_list), np.array(ttft_list)


def _stage_ttft_p50(elapsed_ttft, ttft_s, stage_stats):
    """Per-stage p50 of TTFT observations bucketed by stage time boundaries."""
    vals = []
    for s in stage_stats:
        mask = (elapsed_ttft >= s["s_e"]) & (elapsed_ttft < s["e_e"])
        obs = ttft_s[mask]
        vals.append(float(np.percentile(obs, 50)) if len(obs) > 0 else np.nan)
    return vals


# ── Plot: 8B TTFT timeseries comparison (IPP observations) ────────────────────

def plot_8b_ttft_timeseries(arms, arm_ttft, out_path: Path, offload_arm_name=None):
    """
    Rolling-median timeseries of 8B pod-level TTFT for each arm.
    arm_ttft: {arm_name: (elapsed_array, ttft_s_array)}
    Red strips mark periods when the offload arm was routing to 32B.
    """
    fig, ax = plt.subplots(figsize=FIGSIZE_WIDE)
    ss0 = arms[0]["stage_stats"]
    add_stage_bands(ax, ss0)

    for ai, arm in enumerate(arms):
        data = arm_ttft.get(arm["name"])
        if data is None or len(data[0]) == 0:
            print(f"  WARNING: no TTFT observations for arm {arm['name']!r}")
            continue
        ts_m, ttft_m = data
        color = ARM_COLORS[ai % len(ARM_COLORS)]
        cx, cy = rolling_percentile(ts_m, safe_log(ttft_m), ROLLING_WIN, 50)
        if len(cx):
            ax.plot(cx, cy, color=color, lw=2, label=arm["name"], zorder=4)

    ax.set_yscale("log")
    y_lo, y_hi = ax.get_ylim()

    offload_arm = next((a for a in arms if a["name"] == offload_arm_name), None) \
        if offload_arm_name else None
    if offload_arm is not None:
        big_ts = offload_arm["elapsed"][
            np.array([r["m"] != "small" for r in offload_arm["records"]])
        ]
        periods = _find_active_periods(big_ts)
        if periods:
            strip_top = 10 ** (np.log10(y_lo) + 0.04 * (np.log10(y_hi) - np.log10(y_lo)))
            for lo_p, hi_p in periods:
                ax.fill_between([lo_p, hi_p], y_lo, strip_top,
                                color="#C0504D", alpha=0.75, zorder=5, linewidth=0)
            ax.fill_between([], [], [], color="#C0504D", alpha=0.75,
                            label=f"32B background active ({offload_arm_name})")

    ax.set_ylim(y_lo, y_hi)
    annotate_stage_rps(ax, ss0)
    ax.set_xlabel("elapsed time since 8B Stage-0 (s)  [top labels = 8B stage RPS]")
    ax.set_ylabel("8B TTFT (s, log)")
    ax.set_title("8B TTFT over time (median/{}s): {}".format(
        ROLLING_WIN, " vs ".join(a["name"] for a in arms)), fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, which="both", zorder=0)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out_path}")


# ── Plot: 8B TTFT per-stage pyramid comparison ────────────────────────────────

def plot_8b_ttft_per_stage(arms, arm_ttft, out_path: Path):
    """
    Per-stage p50 of 8B pod-level TTFT for each arm (pyramid x-axis).
    """
    fig, ax = plt.subplots(figsize=(10, 5))

    for ai, arm in enumerate(arms):
        data = arm_ttft.get(arm["name"])
        if data is None or len(data[0]) == 0:
            continue
        ts_m, ttft_m = data
        vals = _stage_ttft_p50(ts_m, ttft_m, arm["stage_stats"])
        color = ARM_COLORS[ai % len(ARM_COLORS)]
        ax.plot(range(len(vals)), safe_log(np.array(vals, dtype=float)),
                marker="o", lw=2, ms=6, color=color,
                label=f"{arm['name']} p50", zorder=3)

    ax.set_yscale("log")
    ss0 = arms[0]["stage_stats"]
    ax.set_xticks(range(len(ss0)))
    ax.set_xticklabels([f"{s['rate']:g}" for s in ss0])
    ax.set_xlabel("stage requested RPS")
    ax.set_ylabel("8B TTFT (s, log)")
    ax.set_title("8B TTFT per stage (p50): pinned vs auto-offload (pyramid)", fontsize=10)
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  Saved {out_path}")


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Experiment 1 analysis plots (multi-arm).")
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--arm", action="append", required=True,
                    metavar="NAME=SUBDIR",
                    help="Repeatable. e.g. --arm scorer=scorer/ --arm 8b-only=8b-only/")
    ap.add_argument("--scorer-arm", default=None,
                    help="Which arm is the scorer (for share/timeseries/prediction plots). "
                         "Default: first --arm.")
    ap.add_argument("--ipp-log", default=None,
                    help="Path to IPP log file. Default: auto-detect from scorer arm dir "
                         "then run-dir root.")
    ap.add_argument("--background", default=None,
                    help="Subdir (relative to --run-dir) of a background workload run "
                         "(own harness_stdout.log + per_request_slim.json) to overlay on "
                         "the traffic-share plot, e.g. a constant low-RPS loop against "
                         "32B. Default: auto-detect '<scorer-arm-dir>/../32b'.")
    ap.add_argument("--out-dir", default=None)
    args = ap.parse_args()

    run_dir = Path(args.run_dir)
    out_dir = Path(args.out_dir) if args.out_dir else run_dir / "plots"
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Loading arms...")
    arms = []
    for spec in args.arm:
        if "=" not in spec:
            sys.exit(f"--arm must be NAME=SUBDIR, got {spec!r}")
        name, sub = spec.split("=", 1)
        arms.append(load_arm(name, run_dir / sub))

    scorer_name = args.scorer_arm or arms[0]["name"]
    scorer = next((a for a in arms if a["name"] == scorer_name), None)
    if scorer is None:
        sys.exit(f"--scorer-arm {scorer_name!r} not among arms")
    print()

    if len(arms) == 1:
        print("NOTE: only one arm given. The headline comparison figures need baselines\n"
              "      (e.g. --arm 8b-only=... --arm static-70-30=...). Producing "
              "single-arm plots only.\n")

    print("Traffic share (scorer arm)...")
    if args.background:
        bg_path = run_dir / args.background
    else:
        bg_path = scorer["path"].parent / "32b"
    background = None
    if bg_path.exists() and (bg_path / "per_request_slim.json").exists():
        try:
            background = load_arm("background-32b", bg_path)
        except (FileNotFoundError, RuntimeError) as e:
            print(f"  WARNING: could not load background workload at {bg_path}: {e}")
    elif args.background:
        print(f"  WARNING: --background path not found: {bg_path}")
    plot_traffic_share(scorer, out_dir / "exp1_traffic_share.png", background=background)

    print("TTFT timeseries (scorer arm)...")
    plot_ttft_timeseries(scorer, out_dir / "exp1_ttft_timeseries.png")

    if len(arms) > 1:
        print("Latency vs load (all arms)...")
        plot_latency_vs_load(arms, out_dir / "exp1_latency_vs_load.png")
        print("Completion rate (all arms)...")
        plot_completions(arms, out_dir / "exp1_completions.png")

        print("8B latency timeseries comparison...")
        offload_name = next(
            (a["name"] for a in arms if any(r["m"] == "big" for r in a["records"])),
            None,
        )
        plot_8b_latency_timeseries(
            arms,
            out_dir / "exp1_8b_latency_timeseries.png",
            offload_arm_name=offload_name,
        )
        print("8B per-stage pyramid comparison...")
        plot_8b_per_stage_pyramid(arms, out_dir / "exp1_8b_per_stage_pyramid.png")

        print("8B TTFT comparison (from IPP ttft-observation logs)...")
        arm_ttft = {}
        for arm in arms:
            ipp = find_ipp_log(arm["path"])
            if ipp:
                el, tt = load_ipp_ttft_obs(ipp, arm["epoch_t0"], model_key="small")
                arm_ttft[arm["name"]] = (el, tt)
                print(f"  [{arm['name']}] {len(el)} 8B TTFT observations from {ipp.name}")
            else:
                print(f"  [{arm['name']}] no IPP log found — skipping TTFT")
        if sum(1 for el, _ in arm_ttft.values() if len(el) > 0) >= 2:
            plot_8b_ttft_timeseries(
                arms, arm_ttft,
                out_dir / "exp1_8b_ttft_timeseries.png",
                offload_arm_name=offload_name,
            )
            plot_8b_ttft_per_stage(arms, arm_ttft, out_dir / "exp1_8b_ttft_per_stage.png")
        else:
            print("  Not enough arms with IPP logs for TTFT comparison — skipping")

    # Auto-detect IPP log: explicit flag > scorer arm dir > run-dir root
    ipp_log = None
    if args.ipp_log:
        ipp_log = Path(args.ipp_log)
    else:
        candidates = [
            scorer["path"] / "ipp-full-live.log",
            scorer["path"].parent / "ipp-full-live.log",
            run_dir / "ipp-full-live.log",
        ]
        for c in candidates:
            if c.exists():
                ipp_log = c
                break

    if ipp_log and ipp_log.exists():
        print(f"Predicted vs actual (loading {ipp_log})...")
        max_el = float(scorer["elapsed"].max())
        ipp = load_ipp_scores(ipp_log, scorer["epoch_t0"], max_el)
        win = sum(1 for s in ipp if s["score"] == 1)
        print(f"  {len(ipp)} score lines, {win} winning decisions")
        plot_predicted_vs_actual(scorer, ipp, out_dir / "exp1_predicted_vs_actual.png")
    else:
        checked = [str(scorer["path"] / "ipp-full-live.log"),
                   str(scorer["path"].parent / "ipp-full-live.log"),
                   str(run_dir / "ipp-full-live.log")]
        print(f"IPP log not found (checked: {', '.join(checked)}) — skipping predicted-vs-actual")

    print("\nDone. Plots written to", out_dir)


if __name__ == "__main__":
    main()

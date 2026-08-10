#!/usr/bin/env python3
"""Actual (harness-measured) TTFT per stage vs requested RPS, one or more runs overlaid.
From inference-perf stage_*_lifecycle_metrics.json (time_to_first_token percentiles).

  plot_ttft_per_stage.py <label>=<stages_dir> [<label>=<dir> ...] [-o out.png] [--title T]

With two runs the printed table shows the % TTFT change of the 2nd vs the 1st (pass the
baseline first), e.g. `pinned=.../8b auto=.../8b` -> reduction of auto vs pinned per stage.
"""
import argparse, glob, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


METRIC = {"ttft": "time_to_first_token", "e2e": "request_latency"}


def stages(d, metric="ttft"):
    out = []
    for f in sorted(glob.glob(f"{d}/stage_*_lifecycle_metrics.json"),
                    key=lambda p: int(p.split("stage_")[1].split("_")[0])):
        x = json.load(open(f))
        t = x["successes"]["latency"][METRIC[metric]]
        out.append({"rps": x["load_summary"]["requested_rate"], "p50": t["median"],
                    "p90": t["p90"], "p99": t["p99"], "n": x["successes"]["count"]})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="label=stages_dir")
    ap.add_argument("-o", "--output", default="ttft_per_stage.png")
    ap.add_argument("--metric", choices=["ttft", "e2e"], default="ttft")
    ap.add_argument("--no-p90", action="store_true")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()
    lbl = {"ttft": "time to first token", "e2e": "end-to-end latency"}[a.metric]
    if a.title is None:
        a.title = f"8B {lbl} per stage (harness-measured)"
    data = {}
    for r in a.runs:
        lab, d = r.split("=", 1)
        s = stages(d, a.metric)
        if not s:
            raise SystemExit(f"no stage_*_lifecycle_metrics.json in {d}")
        data[lab] = s

    fig, ax = plt.subplots(figsize=(10, 5.5))
    colors = ["#9467bd", "#2ca02c", "#1f77b4", "#d62728"]
    for i, (lab, s) in enumerate(data.items()):
        c = colors[i % len(colors)]
        x = list(range(len(s)))
        ax.plot(x, [r["p50"] for r in s], "-o", color=c, label=f"{lab} p50")
        if not a.no_p90:
            ax.plot(x, [r["p90"] for r in s], "--s", color=c, alpha=.5, label=f"{lab} p90")
    first = next(iter(data.values()))
    ax.set_xticks(range(len(first))); ax.set_xticklabels([f"{r['rps']:g}" for r in first])
    ax.set_yscale("log")
    ax.set_xlabel("stage requested RPS"); ax.set_ylabel(f"{lbl} (s, log)")
    ax.set_title(a.title); ax.legend(fontsize=9); ax.grid(alpha=.3, which="both")
    fig.tight_layout(); fig.savefig(a.output, dpi=130)
    print("wrote", a.output)

    labs = list(data)
    for i in range(len(first)):
        rps = first[i]["rps"]
        cells = "  ".join(f"{l} p50={data[l][i]['p50']*1000:.0f}ms" for l in labs if i < len(data[l]))
        row = f"RPS {rps:>4g}: {cells}"
        if len(labs) == 2 and i < len(data[labs[0]]) and i < len(data[labs[1]]):
            base, other = data[labs[0]][i]["p50"], data[labs[1]][i]["p50"]
            if base > 0:
                row += f"   {labs[1]} vs {labs[0]}: {(other-base)/base*100:+.0f}%"
        print(row)


if __name__ == "__main__":
    main()


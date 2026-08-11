#!/usr/bin/env python3
"""Report how the router spread traffic across the leaf clusters, and the
latency/throughput the run saw.

Per-cluster counts come from each leaf EPP's own request histogram, so this is
where traffic actually landed rather than what the router logged. The counters
are cumulative, hence the snapshot/diff pair around a run:

    analyze_split.py --save /tmp/before.json mc-a mc-b
    llmdbenchmark ... run ...
    analyze_split.py --since /tmp/before.json --results <results-dir> mc-a mc-b

Metrics are read through the API server's service proxy, so no port-forward and
no free local port are needed.
"""
import argparse
import json
import pathlib
import subprocess
import sys

COUNTER = "llm_d_epp_request_duration_seconds_count"


def kubectl(*args: str) -> str:
    r = subprocess.run(["kubectl", *args], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"kubectl {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def epp_service(ns: str) -> str:
    """The leaf EPP Service. Its name carries a per-namespace hash, so find it
    by the chart's `-router-epp` suffix rather than hardcoding."""
    for line in kubectl("-n", ns, "get", "svc", "-o", "name").split():
        name = line.split("/", 1)[-1]
        if name.endswith("-router-epp"):
            return name
    sys.exit(f"{ns}: no *-router-epp Service found")


def requests_served(ns: str) -> int:
    path = f"/api/v1/namespaces/{ns}/services/{epp_service(ns)}:9090/proxy/metrics"
    total = 0
    for line in kubectl("get", "--raw", path).splitlines():
        if line.startswith(COUNTER):
            total += int(float(line.rsplit(None, 1)[1]))
    return total


def summarise(results_dir: str) -> None:
    files = sorted(pathlib.Path(results_dir).rglob("summary_lifecycle_metrics.json"))
    if not files:
        print(f"\nno summary_lifecycle_metrics.json under {results_dir}")
        return
    d = json.loads(files[0].read_text())
    ok, lat = d["successes"], d["successes"]["latency"]
    print(f"\nrun summary ({files[0].parent.name})")
    print(f"  requests        {ok['count']} ok / {d['failures']['count']} failed")
    print(f"  wall clock      {d['benchmark_time_seconds']:.1f}s")
    print(f"  throughput      {ok['throughput']['requests_per_sec']:.2f} req/s, "
          f"{ok['throughput']['output_tokens_per_sec']:.0f} out-tok/s")
    for name, key in (("TTFT", "time_to_first_token"), ("latency", "request_latency")):
        m = lat.get(key)
        if m:
            print(f"  {name:<15} mean {m['mean']:.2f}s  p90 {m['p90']:.2f}s")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("namespaces", nargs="+", help="leaf namespaces, one per cluster")
    p.add_argument("--save", metavar="PATH", help="write a counter snapshot and exit")
    p.add_argument("--since", metavar="PATH", help="subtract a snapshot written by --save")
    p.add_argument("--results", metavar="DIR", help="llmdbenchmark results dir to summarise")
    a = p.parse_args()

    now = {ns: requests_served(ns) for ns in a.namespaces}

    if a.save:
        pathlib.Path(a.save).write_text(json.dumps(now))
        print(f"snapshot saved to {a.save}")
        return

    base = json.loads(pathlib.Path(a.since).read_text()) if a.since else {}
    served = {ns: now[ns] - base.get(ns, 0) for ns in a.namespaces}
    total = sum(served.values())

    width = max(len(ns) for ns in a.namespaces)
    print(f"routed {total} requests" + ("" if a.since else " (cumulative, no --since given)"))
    for ns, n in served.items():
        pct = f"{100 * n / total:5.1f}%" if total else "    -"
        print(f"  {ns:<{width}}  {n:6d}  {pct}")

    if a.results:
        summarise(a.results)


if __name__ == "__main__":
    main()

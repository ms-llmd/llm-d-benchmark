#!/usr/bin/env python3
"""Best-effort capture of per-GPU DCGM metrics for a run, into a logs dir.

Called by collect_logs.sh after the per-pod logs are gathered. Derives the run
window from the just-collected decode-pod logs (RFC3339 KV-active timestamps),
queries OpenShift monitoring (Thanos) for the GPU Operator's DCGM counters
scoped to the run's namespace, and writes:

    <logs-dir>/gpu-dcgm/<metric>.json   raw query_range results per metric
    <logs-dir>/gpu-dcgm/summary.txt     per-backend mean(busy)/peak per metric

Never fatal: if oc/Thanos/DCGM are unavailable (e.g. a Kind run), it prints a
notice and exits 0. Auth uses `oc whoami -t` + the thanos-querier route, or env
THANOS_HOST / OC_TOKEN.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

RFC3339 = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?Z")
KV = re.compile(r"GPU KV cache usage:\s*([0-9.]+)%")
METRICS = [
    "DCGM_FI_PROF_PIPE_TENSOR_ACTIVE",
    "DCGM_FI_PROF_SM_ACTIVE",
    "DCGM_FI_PROF_DRAM_ACTIVE",
    "DCGM_FI_PROF_PIPE_FP16_ACTIVE",
    "DCGM_FI_DEV_GPU_UTIL",
    "DCGM_FI_DEV_POWER_USAGE",
]


def oc(*a: str) -> str:
    try:
        return subprocess.run(["oc", *a], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def epoch(line: str):
    m = RFC3339.search(line)
    if not m:
        return None
    base = dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
    return base.timestamp() + (float("0." + m.group(2)) if m.group(2) else 0.0)


def window(logs: Path):
    active = []
    for d in glob.glob(str(logs / "*-decode-*.log")):
        for line in open(d, errors="ignore"):
            k = KV.search(line)
            if k and float(k.group(1)) > 0:
                e = epoch(line)
                if e:
                    active.append(e)
    return (min(active), max(active)) if active else None


def q_range(host, token, query, start, end, step=10):
    p = urllib.parse.urlencode({"query": query, "start": int(start), "end": int(end), "step": step})
    req = urllib.request.Request(f"https://{host}/api/v1/query_range?{p}",
                                 headers={"Authorization": f"Bearer {token}"})
    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
        return json.load(r)["data"]["result"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--logs-dir", required=True)
    ap.add_argument("--namespace", default=os.environ.get("NAMESPACE"), required="NAMESPACE" not in os.environ)
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--pad", type=float, default=30.0)
    args = ap.parse_args()

    logs = Path(args.logs_dir)
    win = window(logs)
    if not win:
        print("collect_dcgm: no decode-log window found; skipping GPU metrics.")
        return 0
    host = os.environ.get("THANOS_HOST") or oc("get", "route", "thanos-querier",
                                               "-n", "openshift-monitoring",
                                               "-o", "jsonpath={.spec.host}")
    token = os.environ.get("OC_TOKEN") or oc("whoami", "-t")
    if not host or not token:
        print("collect_dcgm: no Thanos host/token (not OCP?); skipping GPU metrics.")
        return 0

    out = logs / "gpu-dcgm"
    out.mkdir(exist_ok=True)
    s, e = win[0] - args.pad, win[1] + args.pad
    ns = args.namespace
    _u = lambda ts: dt.datetime.fromtimestamp(ts, dt.timezone.utc)
    summary = [f"# DCGM GPU metrics  window={_u(win[0]):%Y-%m-%dT%H:%M:%S}Z"
               f"..{_u(win[1]):%H:%M:%S}Z  ({win[1]-win[0]:.0f}s)  ns={ns}"]
    got = 0
    for metric in METRICS:
        q = f'{metric}{{exported_namespace="{ns}"}} or {metric}{{namespace="{ns}"}}'
        try:
            res = q_range(host, token, q, s, e, args.step)
        except Exception as ex:
            print(f"collect_dcgm: query failed for {metric}: {ex}")
            continue
        json.dump(res, open(out / f"{metric}.json", "w"))
        got += 1
        scale = 1.0 if metric.endswith("GPU_UTIL") or metric.endswith("POWER_USAGE") else 100.0
        unit = "%" if scale == 100.0 or metric.endswith("GPU_UTIL") else "W"
        for srv in res:
            lbl = srv["metric"]
            pod = lbl.get("exported_pod") or lbl.get("pod", "?")
            gpu = lbl.get("gpu", "?")
            ys = [float(v[1]) * (scale if unit == "%" else 1.0) for v in srv["values"]]
            busy = [y for y in ys if y > 1.0]
            mean_b = sum(busy) / len(busy) if busy else 0.0
            peak = max(ys) if ys else 0.0
            summary.append(f"{metric:32s} gpu={gpu} pod={pod}  mean(busy)={mean_b:6.1f}{unit}  peak={peak:6.1f}{unit}")
    (out / "summary.txt").write_text("\n".join(summary) + "\n")
    print(f"collect_dcgm: wrote {got} metric series + summary to {out}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

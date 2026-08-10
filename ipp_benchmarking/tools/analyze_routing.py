#!/usr/bin/env python3
"""Routing breakdown from IPP "Model selected" decision lines.

Models come from each line's `model` field, so this works for any experiment
(8B/32B, the dual-pool -a/-b aliases, the Kind sims, gemma/qwen). Input may
contain duplicate lines (rolling-snapshot capture overlaps) -> dedup by
x-request-id.  usage: analyze_routing.py <log>
"""
import sys, json

log = sys.argv[1]
sel, ts = {}, {}
for line in open(log, errors="ignore"):
    if '"msg":"Model selected"' not in line:
        continue
    try:
        # collect_logs.sh uses `kubectl logs --timestamps`, which prefixes each
        # line with an RFC3339 stamp; ab_routing_run.sh's capture does not.
        d = json.loads(line[line.find("{"):])
    except ValueError:
        continue
    rid, m = d.get("x-request-id"), d.get("model")
    if not rid or not m:
        continue
    sel[rid] = m
    ts[rid] = float(d.get("ts", 0.0))

models = sorted({m for m in sel.values()})
tot = len(sel) or 1
n_of = lambda rows, m: sum(1 for _, mm in rows if mm == m)
print(f"routing decisions: {len(sel)}   " + "   ".join(
    f"{m}={sum(1 for v in sel.values() if v == m)} "
    f"({100*sum(1 for v in sel.values() if v == m)/tot:.1f}%)" for m in models))

# per-stage split: cluster by idle gaps
rows = sorted((ts[r], sel[r]) for r in sel)
if rows and models:
    stages, cur = [], [rows[0]]
    for prev, r in zip(rows, rows[1:]):
        if r[0] - prev[0] > 8:
            stages.append(cur)
            cur = []
        cur.append(r)
    stages.append(cur)
    print("\nper-stage split:")
    print(f"  {'stage':>5s} {'n':>6s}" + "".join(f" {m[-16:]:>16s} {'%':>6s}" for m in models))
    for i, s in enumerate([s for s in stages if len(s) >= 20]):
        n = len(s)
        print(f"  {i:>5d} {n:6d}" + "".join(
            f" {n_of(s, m):16d} {100*n_of(s, m)/n:5.1f}%" for m in models))

#!/usr/bin/env python3
"""Extract a slim per-request record list from inference-perf's
per_request_lifecycle_metrics.json.

That file keeps the full streamed response per record, so a GPU run produces
several GB (2.9GB for a 11k-request probe) and it is often truncated mid-write,
which makes json.load fail on the whole file. We raw_decode object-by-object off
a bounded buffer and stop at the first incomplete one, so every COMPLETE record
before the tail survives. Per record we keep:
  t    = start_time (monotonic seconds)
  lat  = end_time - start_time
  fail = bool(error)
  ot   = output tokens

    extract_per_request_slim.py <per_request_lifecycle_metrics.json[.gz]|-> <out.json>
"""
import gzip, json, sys


def _open(path):
    if path == "-":
        return sys.stdin
    return (gzip.open if path.endswith(".gz") else open)(path, "rt", errors="ignore")


def extract(path):
    dec = json.JSONDecoder()
    out, buf = [], ""
    f = _open(path)
    while True:
        chunk = f.read(1 << 20)
        buf += chunk
        while True:
            i = buf.find("{")
            if i == -1:
                buf = ""
                break
            try:
                rec, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                buf = buf[i:]
                break  # record spans past the buffer (or is the truncated tail): read more
            st, et = rec.get("start_time"), rec.get("end_time")
            if isinstance(st, (int, float)) and isinstance(et, (int, float)):
                rm = (rec.get("info") or {}).get("response_metrics") or {}
                ot = rm.get("output_tokens")
                if ot is None:
                    ot = (rm.get("server_usage") or {}).get("completion_tokens")
                out.append({"t": st, "lat": et - st, "fail": bool(rec.get("error")), "ot": ot})
            buf = buf[j:]
        if not chunk:
            break
    out.sort(key=lambda r: r["t"])
    return out


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--selfcheck":
        import os, tempfile
        # complete records must survive; the truncated tail must be dropped
        blob = ('[{"start_time":1.0,"end_time":2.0,"error":null,'
                '"info":{"response_metrics":{"output_tokens":42}}},'
                '{"start_time":3.0,"end_time":9.0,"error":"boom"},'
                '{"start_time":5.0,"end')
        p = tempfile.mktemp(suffix=".json")
        open(p, "w").write(blob)
        r = extract(p)
        os.unlink(p)
        assert len(r) == 2, r
        assert r[0]["lat"] == 1.0 and r[0]["ot"] == 42 and not r[0]["fail"], r
        assert r[1]["lat"] == 6.0 and r[1]["fail"] is True, r
        print("selfcheck ok:", r)
        sys.exit(0)
    rows = extract(sys.argv[1])
    json.dump(rows, open(sys.argv[2], "w"))
    print(f"{sys.argv[2]}: {len(rows)} records, {sum(r['fail'] for r in rows)} failed")

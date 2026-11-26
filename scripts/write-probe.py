#!/usr/bin/env python3
"""Write probe for the failover drill: creates uniquely named short links through the gateway.

Usage: write-probe.py --host canary.shortlink.test --seconds 180 --prefix fo1 --out evidence/data/x
Writes <out>.csv (per second: ok / failed writes) and <out>.acked (aliases the API acknowledged
with 201). After a failover, acknowledged aliases missing from the new primary are lost writes (RPO).
"""

import argparse
import collections
import csv
import itertools
import json
import threading
import time
import urllib.error
import urllib.request


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:8480")
    ap.add_argument("--host", required=True)
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    counter = itertools.count(1)
    rows = collections.defaultdict(collections.Counter)
    acked, lock = [], threading.Lock()
    start = time.time()

    def worker():
        while time.time() < start + a.seconds:
            alias = f"{a.prefix}-{next(counter):06d}"
            body = json.dumps({"url": f"https://krmu.edu.in/{alias}", "alias": alias}).encode()
            req = urllib.request.Request(
                f"{a.gateway}/api/links", data=body, method="POST",
                headers={"Host": a.host, "Content-Type": "application/json"},
            )
            t0 = time.time()
            try:
                ok = urllib.request.urlopen(req, timeout=3).status == 201
            except (urllib.error.HTTPError, OSError):
                ok = False
            with lock:
                rows[int(t0 - start)]["ok" if ok else "failed"] += 1
                if ok:
                    acked.append(alias)
            time.sleep(0.05)

    threads = [threading.Thread(target=worker) for _ in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with open(f"{a.out}.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", "epoch", "ok", "failed"])
        for s in range(max(rows) + 1):
            w.writerow([s, int(start + s), rows[s]["ok"], rows[s]["failed"]])
    with open(f"{a.out}.acked", "w") as f:
        f.write("\n".join(sorted(acked)) + "\n")
    failed_secs = [s for s in rows if rows[s]["failed"]]
    window = f"{min(failed_secs)}..{max(failed_secs)}s" if failed_secs else "none"
    print(
        f"write-probe {a.host}: acked={len(acked)} failed={sum(r['failed'] for r in rows.values())} "
        f"failure_window={window} start_epoch={start:.0f}"
    )


if __name__ == "__main__":
    main()

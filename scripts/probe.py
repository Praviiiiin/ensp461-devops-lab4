#!/usr/bin/env python3
"""User-side availability probe for the Envoy gateway (host port 8480).

Usage: probe.py --host canary.shortlink.test --seconds 300 --out evidence/data/x.csv

Creates one short link, then several workers follow it (no redirect following) like users do.
A request is OK when it returns 302; 5xx and connection errors count against availability.
Every response is attributed to the ShortLink version (X-ShortLink-Version) and to the pod
set that served it (X-Served-By: "*-primary-*" = primary/blue, anything else = canary/green).
Writes one CSV row per second and prints a summary line.
"""

import argparse
import collections
import csv
import json
import threading
import time
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


def track(pod: str) -> str:
    return "primary" if "-primary-" in pod else ("canary" if pod else "none")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://localhost:8480")
    ap.add_argument("--host", required=True)
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--pause", type=float, default=0.05, help="sleep between requests per worker")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    hdr = {"Host": a.host}
    req = urllib.request.Request(
        f"{a.gateway}/api/links",
        data=json.dumps({"url": "https://krmu.edu.in/probe"}).encode(),
        headers={**hdr, "Content-Type": "application/json"},
        method="POST",
    )
    code = json.load(urllib.request.urlopen(req, timeout=5))["code"]
    rows = collections.defaultdict(collections.Counter)
    lock = threading.Lock()
    start = time.time()
    stop = start + a.seconds

    def worker():
        while time.time() < stop:
            t0 = time.time()
            version = pod = ""
            try:
                OPENER.open(urllib.request.Request(f"{a.gateway}/{code}", headers=hdr), timeout=3)
                kind = "other"  # a 2xx would be wrong for a redirect
            except urllib.error.HTTPError as e:
                version, pod = e.headers.get("X-ShortLink-Version", ""), e.headers.get("X-Served-By", "")
                kind = "ok" if e.code == 302 else ("err5xx" if e.code >= 500 else "other")
            except OSError:
                kind = "conn_err"
            with lock:
                c = rows[int(t0 - start)]
                c[kind] += 1
                c["slow"] += time.time() - t0 > 0.5
                if version:
                    c[f"v{version}"] += 1
                    c[track(pod)] += 1
                    if kind != "ok":
                        c[f"{track(pod)}_err"] += 1
            time.sleep(a.pause)

    threads = [threading.Thread(target=worker) for _ in range(a.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    base = ["ok", "err5xx", "conn_err", "other", "slow", "primary", "canary", "primary_err", "canary_err"]
    versions = sorted({k for c in rows.values() for k in c if k.startswith("v")})
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["second", "epoch", *base, *versions])
        for s in sorted(rows):
            w.writerow([s, int(start + s), *(rows[s][k] for k in base + versions)])
    tot = collections.Counter()
    for c in rows.values():
        tot.update(c)
    n = tot["ok"] + tot["err5xx"] + tot["conn_err"] + tot["other"]
    bad = sum(1 for c in rows.values() if c["err5xx"] or c["conn_err"] or c["other"])
    by_version = " ".join(f"{v}={tot[v]}" for v in versions)
    print(
        f"probe {a.host} {a.seconds}s: requests={n} ok={tot['ok']} 5xx={tot['err5xx']} "
        f"conn_errors={tot['conn_err']} other={tot['other']} slow(>0.5s)={tot['slow']} "
        f"availability={100 * tot['ok'] / n:.3f}% seconds_with_errors={bad} {by_version}"
    )


if __name__ == "__main__":
    main()

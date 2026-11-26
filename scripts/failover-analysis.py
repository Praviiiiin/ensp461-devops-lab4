"""RTO from the write probe: failover-analysis.py <writes.csv> <crash-epoch>

Write throughput is bucketed by request start second. The outage runs from the crash until the
first second whose acknowledged writes reach 80% of the median rate before the crash.
"""

import csv
import statistics
import sys

rows = list(csv.DictReader(open(sys.argv[1])))
crash = float(sys.argv[2])
start = int(rows[0]["epoch"])  # bucket 0 start (floored)
t = lambda r: start + int(r["second"])  # noqa: E731
before = [int(r["ok"]) for r in rows if t(r) < crash - 1]
normal = statistics.median(before)
after = [r for r in rows if t(r) > crash]
recovered = next(r for r in after if int(r["ok"]) >= 0.8 * normal)
fails = [(int(r["second"]), int(r["failed"])) for r in rows if int(r["failed"])]
print(f"median acknowledged writes/s before crash: {normal}")
print(f"failed writes by start second: {fails}")
print(f"crash at probe second {crash - start:.1f}; full write rate again in second {recovered['second']}")
print(f"RTO (crash -> normal write rate) ~ {t(recovered) - crash:.1f}s")

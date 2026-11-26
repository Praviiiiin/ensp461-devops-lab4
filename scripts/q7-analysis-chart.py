"""Q7 chart: Flagger analysis metrics vs thresholds, drawn from evidence/logs/q7-metric-analysis.log
and the flagger_canary_weight range queries in evidence/data. Usage: uv run --with matplotlib scripts/q7-analysis-chart.py . evidence/charts/q7-analysis-metrics.png"""

import datetime as dt
import json
import re
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

root, out = sys.argv[1], sys.argv[2]
plt.rcParams.update({"font.family": "Helvetica", "font.size": 10, "axes.spines.top": False})
BLUE, RED, GREEN, GREY = "#2563eb", "#dc2626", "#16a34a", "#6b7280"
DAY = "2026-11-26 "


def ts(hms):
    return dt.datetime.strptime(DAY + hms, "%Y-%m-%d %H:%M:%S")


log = open(f"{root}/evidence/logs/q7-metric-analysis.log").read().split("\n")
series = {}
for i, line in enumerate(log):
    m = re.match(r"== (\w+): ([\w-]+) MetricTemplate", line)
    if m:
        series[(m[1], m[2])] = [(ts(p.split("=")[0]), float(p.split("=")[1])) for p in log[i + 1].split()]


def weights(name):
    for r in json.load(open(f"{root}/evidence/data/q7-canary-{name}-weights.json"))["data"]["result"]:
        if r["metric"]["workload"] == "shortlink":
            return [(dt.datetime.fromtimestamp(float(t), dt.UTC).replace(tzinfo=None), float(v)) for t, v in r["values"]]


fig, (a, b) = plt.subplots(1, 2, figsize=(11, 4.9))

# Rollback drill
e = series[("rollback", "error-rate-percent")]
a.plot([p[0] for p in e], [p[1] for p in e], color=RED, marker="o", ms=3, lw=1.6, label="canary error-rate-percent (1 m)")
a.axhline(1, color=RED, ls="--", lw=1, label="threshold max 1 %")
w = [p for p in weights("rollback") if ts("10:49:00") <= p[0] <= ts("10:52:30")]
a2 = a.twinx()
a2.step([p[0] for p in w], [p[1] for p in w], where="post", color=BLUE, lw=1.4, label="canary weight %")
a2.set_ylim(0, 60)
a2.set_ylabel("canary weight %", color=BLUE)
for t, txt in [("10:49:48", "acceptance\nwebhook failed\n(check 1)"), ("10:50:48", "18.72 > 1\n(check 2)"),
               ("10:51:18", "rollback,\ncanary\nscaled to 0")]:
    a.axvline(ts(t), color=GREY, lw=0.8, ls=":")
    a.text(ts(t), 36.5, txt, fontsize=8, color=GREY, ha="center", va="top",
           bbox={"fc": "white", "ec": "none", "pad": 1})
a.set_xlim(ts("10:49:00"), ts("10:52:30"))
a.text(ts("10:51:40"), 11, "1 m rate window\nstill trailing", fontsize=8, color=GREY)
a.set_ylim(0, 38)
a.set_ylabel("error rate %")
a.set_title("Rollback drill (2.1.0-fault, 20 % injected 5xx)", loc="left", fontsize=11)
h1, l1 = a.get_legend_handles_labels()
h2, l2 = a2.get_legend_handles_labels()
a.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=8.5, ncol=2, bbox_to_anchor=(0, -0.2))

# Promotion 2.1.0 -> 2.2.0
p95 = series[("promote", "p95-latency-ms")]
err = series[("promote", "error-rate-percent")]
b.plot([p[0] for p in p95], [p[1] for p in p95], color=GREEN, marker="o", ms=3, lw=1.6, label="canary p95-latency-ms (1 m)")
b.plot([p[0] for p in err], [p[1] for p in err], color=RED, marker="o", ms=3, lw=1.2, label="canary error-rate-percent (1 m)")
b.axhline(250, color=GREEN, ls="--", lw=1, label="p95 threshold max 250 ms")
b.set_yscale("symlog", linthresh=1)
b.set_ylim(-0.2, 1000)
b.set_ylabel("ms (p95) / % (errors), symlog")
w = [p for p in weights("promote") if ts("10:56:00") <= p[0] <= ts("11:01:00")]
b2 = b.twinx()
b2.step([p[0] for p in w], [p[1] for p in w], where="post", color=BLUE, lw=1.4, label="canary weight %")
b2.set_ylim(0, 60)
b2.set_ylabel("canary weight %", color=BLUE)
b.set_xlim(ts("10:56:00"), ts("11:01:00"))
b.set_title("Promotion 2.1.0 -> 2.2.0", loc="left", fontsize=11)
h1, l1 = b.get_legend_handles_labels()
h2, l2 = b2.get_legend_handles_labels()
b.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=8.5, ncol=2, bbox_to_anchor=(0, -0.2))

for ax in (a, b):
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%H:%M:%S"))
    ax.set_xlabel("time (UTC)")
fig.tight_layout()
fig.savefig(out, dpi=150)

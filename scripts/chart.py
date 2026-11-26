"""Charts for the traffic experiments, drawn only from recorded evidence.

  uv run --with matplotlib scripts/chart.py canary  <probe.csv> <weights.json> <out.png> <title> [new-version]
  uv run --with matplotlib scripts/chart.py bluegreen <probe.csv> <out.png> <title>
  uv run --with matplotlib scripts/chart.py failover <writes.csv> <out.png> <title>

probe.csv comes from scripts/probe.py, weights.json is a Prometheus query_range answer for
flagger_canary_weight, writes.csv comes from scripts/write-probe.py.
"""

import csv
import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

plt.rcParams.update({"font.family": "Helvetica", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
BLUE, ORANGE, RED, GREY = "#2563eb", "#ea580c", "#dc2626", "#6b7280"


def rows(path):
    with open(path) as f:
        return [{k: (int(v) if v.lstrip("-").isdigit() else v) for k, v in r.items()} for r in csv.DictReader(f)]


def smooth(xs, n=5):
    return [sum(xs[max(0, i - n + 1) : i + 1]) / len(xs[max(0, i - n + 1) : i + 1]) for i in range(len(xs))]


def canary(probe, weights, out, title, new_version=None):
    r = rows(probe)
    t0 = r[0]["epoch"]
    t = [x["second"] for x in r]
    share = smooth([100 * x["canary"] / max(1, x["canary"] + x["primary"]) for x in r])
    fig, (a, b) = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    a.plot(t, share, color=ORANGE, lw=1.6, label="observed share served by canary pods (5 s mean)")
    if new_version:
        ok = [max(1, x["ok"] + x["err5xx"] + x["other"]) for x in r]
        a.plot(t, smooth([100 * x[f"v{new_version}"] / n for x, n in zip(r, ok)]), color="black", ls=":", lw=1.2,
               label=f"share answered by v{new_version} (any pod set)")
    for res in json.load(open(weights))["data"]["result"]:
        if res["metric"].get("workload") == "shortlink":
            pts = [(float(ts) - t0, float(v)) for ts, v in res["values"] if 0 <= float(ts) - t0 <= t[-1]]
            a.step([p[0] for p in pts], [p[1] for p in pts], where="post", color=BLUE, lw=1.4, label="Flagger canary weight")
    a.set_ylabel("% of user requests")
    a.set_ylim(-3, 103)
    a.legend(loc="upper left", frameon=False)
    a.set_title(title, loc="left", fontsize=11)
    b.bar(t, [x["primary_err"] for x in r], color=GREY, width=1, label="5xx from primary")
    b.bar(t, [x["canary_err"] for x in r], bottom=[x["primary_err"] for x in r], color=RED, width=1, label="5xx from canary")
    b.set_ylabel("errors / s")
    b.set_ylim(0, max(1, max(x["primary_err"] + x["canary_err"] for x in r)) * 1.2)
    b.set_xlabel("seconds since probe start")
    b.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=150)


def bluegreen(probe, out, title):
    r = rows(probe)
    t = [x["second"] for x in r]
    versions = [k for k in r[0] if k.startswith("v")]
    fig, (a, b) = plt.subplots(2, 1, figsize=(9, 5.6), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    a.stackplot(
        t,
        [x["primary"] for x in r],
        [x["canary"] for x in r],
        colors=[BLUE, "#16a34a"],
        labels=["served by primary pods", "served by green (canary) pods"],
        alpha=0.85,
    )
    for v, style in zip(versions, ["--", ":"]):
        a.plot(t, [x[v] for x in r], color="black", ls=style, lw=1, label=f"responses from {v}")
    a.set_ylabel("responses / s")
    a.legend(loc="lower left", frameon=False, fontsize=9)
    a.set_title(title, loc="left", fontsize=11)
    b.bar(t, [x["err5xx"] for x in r], color=RED, width=1, label="5xx")
    b.bar(t, [x["conn_err"] for x in r], bottom=[x["err5xx"] for x in r], color=GREY, width=1, label="connection errors")
    b.set_ylabel("errors / s")
    b.set_ylim(0, max(1, max(x["err5xx"] + x["conn_err"] for x in r)) * 1.2)
    b.set_xlabel("seconds since probe start")
    b.legend(loc="upper left", frameon=False)
    fig.tight_layout()
    fig.savefig(out, dpi=150)


def failover(path, out, title):
    r = rows(path)
    t = [x["second"] for x in r]
    fig, a = plt.subplots(figsize=(9, 3.6))
    a.bar(t, [x["ok"] for x in r], color=BLUE, width=1, label="acknowledged writes")
    a.bar(t, [x["failed"] for x in r], bottom=[x["ok"] for x in r], color=RED, width=1, label="failed writes")
    a.set_ylabel("writes / s")
    a.set_ylim(0, max(x["ok"] + x["failed"] for x in r) * 1.45)
    a.set_xlabel("seconds since probe start")
    a.legend(loc="upper right", frameon=False)
    a.set_title(title, loc="left", fontsize=11)
    fig.tight_layout()
    fig.savefig(out, dpi=150)


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    {"canary": canary, "bluegreen": bluegreen, "failover": failover}[mode](*args)

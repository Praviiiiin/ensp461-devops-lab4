# ENSP461 Lab 4: Advanced Deployment Strategies & Enterprise DevOps

E Pravin, 2301730215, B.Tech CSE (AI & ML), K.R. Mangalam University, Gurugram.

ShortLink (Flask + PostgreSQL URL shortener) delivered with GitHub Actions gates, Flux GitOps, Flagger canary and blue-green on Envoy Gateway, SOPS/age secrets and Velero disaster recovery, all on a local kind cluster.
The full report is [report/lab4.md](report/lab4.md).

## What is where

| Path | Contents |
|---|---|
| `app/` | ShortLink 2.x source, unit tests and PostgreSQL integration tests |
| `Dockerfile` | non-root, read-only-friendly runtime image |
| `.github/workflows/ci.yml` | Q2 pipeline: lint/unit, integration, gitleaks gates; arm64 build to GHCR; smoke-tested deploy gate |
| `platform/` | kind cluster definition, `bootstrap.sh` (kind + age key + Flux), `garage.sh` (off-cluster S3), `velero.sh` |
| `clusters/pv-lab4/` | Flux components, the GitRepository and the four Kustomization layers |
| `infrastructure/` | Envoy Gateway, Flagger + load tester, Prometheus, Grafana, metrics-server; Gateway, MetricTemplates, dashboard |
| `apps/data/` | PostgreSQL primary/standby pair, Velero hooks, SOPS-encrypted credentials |
| `apps/workloads/` | `canary` (sl-canary) and `bluegreen` (sl-bg) environments with their Flagger Canary objects |
| `scripts/` | evidence recorder, probes, drills, chart and screenshot helpers |
| `evidence/` | `logs/` real command output, `data/` probe CSVs and Prometheus ranges, `charts/`, `screenshots/` |
| `report/` | `lab4.md` (built from `report/parts/`), SVG diagrams |

## How to run each part

Prerequisites: Docker, kind, kubectl, helm, flux, velero, sops, age, gitleaks, uv.
All commands run from the repository root; `scripts/rec.sh <step> '<command>'` records any command into `evidence/logs/<step>.log`.

```sh
# platform (needs .secrets/age.agekey, the SOPS private key, which is not in Git)
./platform/bootstrap.sh        # kind cluster pv-lab4 + Flux; Flux installs everything else from Git
./platform/garage.sh           # pv-garage S3 container on the kind network
./platform/velero.sh           # Velero + node agent against Garage

# Q2: push a change under app/ and watch the pipeline
gh run list -R Praviiiiin/ensp461-devops-lab4

# Q4 / Q7 / Q3: promote a CI image by editing newTag in apps/workloads/<env>/kustomization.yaml, push, then
python3 scripts/probe.py --host canary.shortlink.test --seconds 600 --out evidence/data/x.csv
./scripts/watch-canary.sh sl-canary          # or sl-bg

# Q6
./scripts/secret-audit.sh && gitleaks git . --redact

# Q1
./scripts/dr-backup-restore.sh dr-drill-N   # Velero backup, restore into sl-data-dr, fingerprint check
./scripts/pg-failover-drill.sh              # crash the primary under writes, automatic promotion
```

## Results at a glance

See the per-question sections of [report/lab4.md](report/lab4.md) for the evidence behind each number.

| Question | Result |
|---|---|
| Q1 DR | Velero restore into a fresh namespace verified row-for-row in 31.7 s (RPO = 15 min schedule); primary crash failover RTO 5.6 s, 0 of 6,995 acknowledged writes lost |
| Q2 CI/CD gates | failing unit test blocked build and deploy (run 45798321003); fix went green through all gates and the smoke-tested deploy gate (run 45798477435) |
| Q3 blue-green | Gateway API flip 2.2.0 to 2.3.0: 32,417 requests, 0 errors, versions mixed for 1 s; faulty green rejected with 0 user requests reaching it |
| Q4 GitOps | Git change live in 118 s, `git revert` undone in 103 s, deleted HTTPRoute restored by Flux in 116 s |
| Q6 secrets | 0 plaintext secret values in Git history, cluster values match the encrypted ones, gitleaks clean, CI secrets masked |
| Q7 canary | 2.1.0 to 2.2.0 in 10% steps to 50%: 38,670 requests, 0 errors; faulty canary rolled back at 10% after error rate 18.72% |

# Lab 4: Advanced Deployment Strategies & Enterprise DevOps

**Course:** ENSP461 DevOps & Automation
**Student:** E Pravin, roll number 2301730215, B.Tech CSE (AI & ML), K.R. Mangalam University, Gurugram
**Repository:** https://github.com/Praviiiiin/ensp461-devops-lab4

This lab takes ShortLink, the Flask and PostgreSQL URL shortener from my capstone, and runs it under four advanced delivery mechanisms on a local Kubernetes cluster.
Every number, log excerpt, chart and screenshot in this report comes from real runs on my machine on 26 November 2026; the raw logs are in `evidence/logs/`, the measured data in `evidence/data/`, and the charts in `evidence/charts/`.

The environment is an Apple Silicon (arm64) Mac with Docker provided by OrbStack.
The cluster is a single-node kind cluster named `pv-lab4` (Kubernetes v1.37.0) on its own Docker network `pv-lab4-net`; OrbStack's built-in Kubernetes stays switched off.
The tool chain is deliberately different from the capstone's hand-written blue-green script:

| Concern | Tooling used in this lab |
|---|---|
| CI/CD gates (Q2) | GitHub Actions on the public repository, arm64 runner, GHCR registry |
| GitOps (Q4) | Flux v2 (source, kustomize and helm controllers), repository as the only source of truth |
| Traffic management | Envoy Gateway v1.10 implementing the Kubernetes Gateway API |
| Canary and blue-green (Q7, Q3) | Flagger 1.45 with Prometheus metric gates and the Flagger load tester |
| Secrets (Q6) | SOPS with age, decrypted by Flux; GitHub Actions encrypted secrets; gitleaks |
| Disaster recovery (Q1) | Velero 1.19 with Kopia file-system backup to an off-cluster Garage S3 store; PostgreSQL 17 streaming replica with a scripted promotion |
| Observability | Prometheus (5 s scrape) and Grafana |

![Figure: Lab 4 architecture: GitHub holds code, manifests and encrypted secrets; Flux reconciles the kind cluster; Envoy Gateway and Flagger steer users between versions; Velero ships backups to Garage outside the cluster](diagrams/architecture.svg)

Times in command output are UTC unless a line says IST (the Mac's local time zone, UTC+05:30).
Users are simulated by two probes that run on the Mac and enter through the gateway on `localhost:8480` exactly like a browser would: `scripts/probe.py` follows a short link about 65 times per second and records which version and which pod set answered, and `scripts/write-probe.py` creates uniquely named links and remembers which ones the API acknowledged.

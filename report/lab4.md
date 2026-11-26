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

# Q1. Disaster Recovery: Backup, Restore and Failover

> **Question.** Design and demonstrate a disaster recovery mechanism (backup, restore, failover) and document recovery objectives and observations.

## Design and recovery objectives

ShortLink's state is the PostgreSQL data tier in namespace `sl-data`; everything else (code, manifests, encrypted secrets) is in Git and is rebuilt by Flux (Q4).
Two complementary mechanisms protect the data, each for a different class of disaster:

| Disaster | Mechanism | Recovery point objective (RPO) | Recovery time objective (RTO) |
|---|---|---|---|
| Primary database crash or node loss | PostgreSQL 17 hot standby `pg-b-0` with asynchronous streaming replication, promoted by `scripts/pg-failover.sh` | near 0 (unreplicated WAL only) | under 30 s |
| Loss of the namespace, the cluster, or logical corruption | Velero backup of `sl-data` to the off-cluster Garage S3 store, restored into a fresh namespace | 15 min (backup schedule) | under 5 min |

The targets were set before the drills; the measured values are in the results below.

**Backup and restore.**
Velero 1.19 runs in the cluster with its node agent (Kopia) and writes to bucket `velero` in `pv-garage`, a Garage S3 server in a Docker container outside the cluster, so the backups survive deleting the whole kind cluster.
MinIO was the suggested store, but its container images are no longer published (both `quay.io/minio/minio` and `minio/minio` returned access denied on 26 November 2026), so Garage, a lightweight S3-compatible server, replaced it.
Copying a running PostgreSQL data directory file by file is not crash-consistent, so the backup carries a logical dump instead:

1. A Velero pre-backup hook on each Postgres pod runs `backup-dump.sh`; on the current primary it writes `pg_dump -Fc` files for `sl_canary` and `sl_bg` into the `dumps` volume, on the standby it does nothing.
2. Velero stores the namespace manifests and, through Kopia, only the opted-in `dumps` volume; the data PVCs are excluded.
3. On restore, the StatefulSets create fresh volumes, the primary runs `initdb` and creates roles and databases, and a post-restore hook runs `pg_restore` from the restored dump; the standby clones the restored primary with `pg_basebackup`.

**Failover.**
`pg-b-0` streams WAL from `pg-a-0`.
The `pg` Service that the application uses selects the writable member by pod name, and which member is primary is declared in Git (`PRIMARY_HOST` empty on the primary).
`scripts/pg-failover.sh` checks the primary from the standby once per second; after three consecutive failures it fences the old primary (scales its StatefulSet to 0), promotes the standby with `pg_promote()`, and repoints the Service.
Flux is suspended for the data layer during the incident, otherwise it would restore the old selector and revive the old primary; afterwards the new topology is committed to Git.

![Figure: Q1 design: logical dumps shipped by Velero to off-cluster Garage and restored into a fresh namespace; crash of the primary handled by promoting the streaming standby](diagrams/q1-dr.svg)

## Key configuration

The hooks are pod annotations, so they travel with the GitOps-managed StatefulSet:

```yaml
# apps/data/postgres.yaml (pod template annotations, both members)
        backup.velero.io/backup-volumes: dumps
        pre.hook.backup.velero.io/container: postgres
        pre.hook.backup.velero.io/command: '["/bin/sh", "/scripts/backup-dump.sh"]'
        post.hook.restore.velero.io/container: postgres
        post.hook.restore.velero.io/command: '["/bin/sh", "/scripts/restore-dump.sh"]'
```

```sh
# apps/data/scripts.yaml: backup-dump.sh
if [ "$(psql -U postgres -Atc 'select pg_is_in_recovery()')" = t ]; then echo "standby: no dump"; exit 0; fi
for db in sl_canary sl_bg; do pg_dump -U postgres -Fc -d "$db" -f "/dumps/$db.dump"; done
date -u +%FT%TZ > /dumps/taken-at
```

The failover controller:

{{file:scripts/pg-failover.sh}}

Velero points at Garage through the Docker network name; `publicUrl` lets the CLI on the Mac read backup logs through the published port:

```sh
# platform/velero.sh (excerpt)
velero install --provider aws --image velero/velero:v1.19.0 \
  --plugins velero/velero-plugin-for-aws:v1.15.0 --bucket velero \
  --backup-location-config region=garage,s3ForcePathStyle=true,s3Url=http://pv-garage:3900,publicUrl=http://127.0.0.1:3900 \
  --use-volume-snapshots=false --use-node-agent --default-volumes-to-fs-backup=false
```

## Drill A: backup and restore into a fresh namespace

`scripts/dr-backup-restore.sh` seeds 5,000 links, fingerprints both databases (row count plus an MD5 over all codes and URLs), takes a Velero backup, writes 250 more links after the backup, restores into the new namespace `sl-data-dr`, and polls the restored primary until its fingerprint matches.

**Verified output** (evidence/logs/q1-backup-restore.log, drill `dr-drill-4`):

```text
fingerprint before backup: sl_canary 5258 rows, md5 f92533ff21a5053e222f13cbce80aa01 | sl_bg 4 rows, md5 1718079526ec9e23cde4b1616e3182eb
Backup completed with status: Completed.
Total items to be backed up:  55
  Pod Volume Backups - kopia:
      sl-data/pg-a-0: dumps (size: 32133, incremental size: 32133)
      sl-data/pg-b-0: dumps (size: 0, incremental size: 0)
msg="stdout: ... 1862 Nov 26 11:22 sl_bg.dump ... 30250 Nov 26 11:22 sl_canary.dump ... taken-at\n" container=postgres phase=pre
msg="stdout: standby: no dump\n" container=postgres phase=pre
== 11:22:42 250 writes after the backup (they are outside its recovery point)
== 11:22:42 restore into fresh namespace sl-data-dr
Restore completed with status: Completed.
Items restored:              22
HooksAttempted:   2
HooksFailed:      0
fingerprint restored:      sl_canary 5258 rows, md5 f92533ff21a5053e222f13cbce80aa01 | sl_bg 4 rows, md5 1718079526ec9e23cde4b1616e3182eb
sl_bg matches
source now has 5508 rows; restored copy lacks the 250 post-backup rows (RPO = time since backup)
backup took 10.9s; restore API call 31.4s; restore-to-verified-data (RTO) 31.7s
walreceiver|streaming
dr00042|https://krmu.edu.in/dr/42
Schedule "sl-data-15m" created successfully.
```

The restored database is byte-for-byte equivalent at the logical level (same row count and the same MD5 over every row), the restored standby is streaming from the restored primary, a seeded link (`dr00042`) is present and the post-backup link `late-4-0001` is absent, exactly as the recovery point predicts.
The schedule `sl-data-15m` (every 15 minutes, kept 72 h) bounds the RPO of this path to 15 minutes.

Two earlier attempts are kept in the same log because they show why the verification step matters.
`dr-drill-2` ran while Velero was re-creating its Kopia repository after I added `publicUrl`; the backup ended `PartiallyFailed` without the dump volume, the restore still reported `Completed`, and only the fingerprint loop revealed that the restored database was empty.
`dr-drill-3` stopped on a bug in my phase check (an unsupported `velero -o jsonpath` flag), which is now done with `kubectl`.

## Drill B: crash of the primary under write load

`scripts/pg-failover-drill.sh` suspends Flux for the data layer, starts the write probe (three workers creating uniquely named links through the gateway, about 48 acknowledged writes per second), starts the failover controller, and after 40 s sends SIGKILL to the `pg-a-0` postmaster on the node and scales its StatefulSet to 0 (the node is treated as lost).

**Verified output** (evidence/logs/q1-failover.log):

```text
11:35:24.823 replication before the crash:
walreceiver|streaming|async|0
11:35:25.400 CRASH: SIGKILL to the pg-a-0 postmaster (host pid 88893), node treated as lost (statefulset scaled to 0)
11:35:26.159 primary check failed (1/3)
11:35:27.275 primary check failed (2/3)
11:35:28.378 primary check failed (3/3)
11:35:28.395 fencing: scale statefulset/pg-a to 0
11:35:28.451 promoting pg-b-0: t
11:35:28.792 service pg now selects pg-b-0; in recovery: f
write-probe canary.shortlink.test: acked=6995 failed=10 failure_window=40..57s start_epoch=1795692885
 acked | present | lost
-------+---------+------
  6995 |    6995 |    0
median acknowledged writes/s before crash: 48
failed writes by start second: [(40, 9), (57, 1)]
RTO (crash -> normal write rate) ~ 5.6s
```

| Measure | Value |
|---|---|
| Replication lag at the crash | 0 bytes |
| Crash to promoted primary behind the Service | 3.4 s (3 checks at 1 s, then 0.4 s to fence, promote and repoint) |
| RTO, crash to normal write rate | 5.6 s (probe resolution 1 s) |
| Failed writes | 10 of 7,005 (9 in flight at the crash, 1 stale pooled connection 16 s later) |
| RPO, acknowledged writes lost | 0 of 6,995 |

![Figure: Q1 failover drill: acknowledged and failed writes per second around the crash of the primary](../evidence/charts/q1-failover-writes.png)

The application needed no restart: psycopg's pool discarded the broken connections and new connections went through the `pg` Service to the promoted member.
The one late failure at second 57 was a pooled connection to the dead primary that was only used then; a pool health check (`check=ConnectionPool.check_connection`) would remove that tail.

### Making the failover permanent through Git

Commit `4f1c451` declares `pg-b` as primary (empty `PRIMARY_HOST`, Service `pg` selects `pg-b-0`) and `pg-a` as a standby seeded from `pg-b-0`.
Resuming Flux exposed a real runbook pitfall.

**Verified output** (evidence/logs/q1-failover-rejoin.log):

```text
finding: flux resume reconciled the cached pre-commit revision first, so pg-a came back with an empty volume as a standalone primary (fenced: Service pg already pointed at pg-b-0). Redoing the rejoin in the safe order: freeze, scale to 0, drop the volume, then resume on the current revision.
flux-system	main@sha1:4f1c4514	False    	True 	stored artifact for revision 'main@sha1:4f1c4514'
✔ applied revision main@sha1:4f1c4514a979dbcc1f92da1975880ae05c747d16
40557/40557 kB (100%), 1/1 tablespace
walreceiver|streaming|async
t
12506
12506
pg           ClusterIP   10.96.62.69    <none>        5432/TCP   94m   app=pg,statefulset.kubernetes.io/pod-name=pg-b-0
pg-standby   ClusterIP   10.96.103.49   <none>        5432/TCP   94m   app=pg,statefulset.kubernetes.io/pod-name=pg-a-0
```

The old primary rejoined as a hot standby (`pg_is_in_recovery() = t`) with the same 12,506 links as the new primary, and Git, the cluster and the database roles agree again.

## Observations

- **Both objectives were met with margin.** Failover: RPO 0 and RTO 5.6 s against targets of near 0 and 30 s. Restore: RPO bounded by the 15-minute schedule and RTO 31.7 s against 5 minutes.
- **Asynchronous replication makes RPO 0 a result, not a guarantee.** The lag was 0 bytes at the crash because the write rate is small; under heavy load the last transactions acknowledged by the primary could be lost. `synchronous_standby_names` would guarantee RPO 0 at the cost of write latency and of blocking writes when the standby is down.
- **Restore RTO grows with data size.** A logical dump of 32 kB restores in seconds; a large database would need physical base backups plus WAL archiving (point-in-time recovery) to keep the RTO and to reach an RPO below the schedule interval.
- **Verification is part of the backup.** Velero reported `Completed` for a restore of an empty database (`dr-drill-2`); only the fingerprint comparison caught it.
- **GitOps and imperative recovery must be sequenced.** Flux has to be frozen during the failover and the new topology committed before it is resumed, and the new revision must be fetched before resuming, or Flux applies the stale desired state.
- **Fencing by routing.** The Service selects one pod by name, so a revived old primary receives no traffic; this prevented split-brain writes when `pg-a` briefly came back as a standalone primary.
- **Backups contain secrets.** The Velero backup includes the decrypted `pg-credentials` Secret, so the Garage bucket must be protected like the cluster itself (see Q6).
- **Single-node limits.** Everything runs on one kind node, so a real node or zone failure could not be simulated; the crash was a SIGKILL of the postmaster plus scaling its StatefulSet to 0.

# Q2. Automated Testing as CI/CD Pipeline Gates

> **Question.** Implement automated testing within a CI/CD pipeline, integrating unit/integration tests as pipeline gates before deployment.

## Design

The pipeline is a single GitHub Actions workflow, `.github/workflows/ci.yml`, in the public repository `Praviiiiin/ensp461-devops-lab4`.
It runs on every push to `main` that touches the application, the Dockerfile or the workflow itself, on every pull request, and on manual dispatch.
Three independent gates run in parallel on GitHub-hosted runners, and nothing is built unless all three are green.

- **Gate 1, lint and unit tests.** `ruff check` and `ruff format --check` enforce style and a security-flavoured rule set (`E, F, W, I, B, S, UP`), then `pytest -m "not integration"` runs the unit suite against an in-memory fake store with a hard coverage floor of 80 %.
- **Gate 2, integration tests.** The same code is tested against a real PostgreSQL 17 service container, so SQL, schema creation, unique-key handling and deletes are exercised end to end.
  The database password comes from the repository secret `CI_DB_PASSWORD`, never from the workflow file.
- **Gate 3, secret scan.** gitleaks v8.28.0 scans the full Git history (`fetch-depth: 0`), so a credential committed and later deleted still fails the build.

Only when the three gates pass does the `build` job (`needs: [unit, integration, secret-scan]`) build the arm64 image with Buildx on an `ubuntu-24.04-arm` runner and push it to GHCR as `ghcr.io/praviiiiin/shortlink-lab4:<version>-<sha7>`.
The final `deploy-gate` job runs only for pushes, inside the protected GitHub environment `lab4-cluster`.
That environment only accepts the `main` branch and holds every deployment for a one minute wait timer.
The job then pulls the image that was just pushed, by its immutable digest rather than its tag, runs it next to a fresh PostgreSQL, and executes a black-box smoke test over HTTP.
A green deploy gate is the precondition for promotion: the image tag is then committed to `apps/workloads/*/kustomization.yaml`, and Flux and Flagger take over (Q4 and Q7).
CI never holds cluster credentials, so a broken commit cannot reach the cluster even if someone skips the promotion step.

![Figure: Q2 pipeline: three parallel test gates, an image build that needs all of them, and a protected deploy gate that smoke-tests the pushed image by digest](diagrams/q2-pipeline.svg)

## Key configuration

The complete workflow:

{{file:.github/workflows/ci.yml}}

The test split is done with a pytest marker.
Integration tests carry the `integration` marker and skip themselves when no database is configured, so a developer can run the fast suite locally without PostgreSQL:

```python
# app/tests/test_integration.py
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.getenv("PGHOST"), reason="needs PostgreSQL")]

def test_round_trip_persists_hits(client):
    code = client.post("/api/links", json={"url": "https://www.postgresql.org/docs/"}).get_json()["code"]
    for _ in range(3):
        assert client.get(f"/{code}").status_code == 302
    stats = client.get(f"/api/links/{code}").get_json()
    assert stats["hits"] == 3 and stats["url"] == "https://www.postgresql.org/docs/"
```

The unit suite pins the URL validation rules with a table of cases, including the 2048-character limit that the failing run below caught:

```python
# app/tests/test_app.py
@pytest.mark.parametrize("url,ok", [
    ("https://example.com/a?b=c", True),
    ("ftp://example.com", False),
    ("javascript:alert(1)", False),
    pytest.param("https://x.io/" + "a" * 2100, False, id="longer-than-2048"),
    ("https://user:pass@example.com/", False),
    ...
])
def test_valid_url(url, ok):
    assert valid_url(url) is ok
```

The protection rules of the `lab4-cluster` environment, read back from the GitHub API:

**Verified output** (evidence/logs/q2-run-timings.log):

```text
{"deployment_branch_policy":{"custom_branch_policies":true,"protected_branches":false},"name":"lab4-cluster",
 "protection_rules":[{"type":"wait_timer","wait_timer":1},{"type":"branch_policy","wait_timer":null}]}
["main"]
```

## Experiment 1: a regression is stopped at gate 1

Release 2.1.0 (commit `64fd9be`) rewrote `valid_url()` to strip whitespace and reject URLs that embed credentials.
The rewrite silently dropped the `len(url) > MAX_URL_LEN` check.
The push started run 45798321003, and gate 1 failed on exactly the parametrised case that guards that limit.
Gates 2 and 3 still finished green, because they test other properties, but `build` and `deploy-gate` were skipped, so no image for the broken commit was ever published.

**Verified output** (evidence/logs/q2-red-run.log):

```text
X main ci · 45798321003
JOBS
✓ gate 3 - gitleaks (full history) in 8s (ID 139834632010)
X gate 1 - lint and unit tests in 18s (ID 139834632485)
  ✓ Run ruff check . && ruff format --check .
  X Run pytest -m "not integration" --cov --cov-report=term-missing --cov-fail-under=80 -q
✓ gate 2 - integration tests (PostgreSQL) in 22s (ID 139834632554)
- build and push arm64 image in 0s (ID 139834861822)
- deploy gate - smoke test pushed image in 0s (ID 139834863448)

_______________________ test_valid_url[longer-than-2048] _______________________
>       assert valid_url(url) is ok
E       AssertionError: assert True is False
FAILED tests/test_app.py::test_valid_url[longer-than-2048] - AssertionError: assert True is False
1 failed, 26 passed, 4 deselected in 0.91s
```

The fix (`fbefde2`, "Restore the 2048-character URL limit dropped in 2.1.0") restored the one condition.
Its run 45798477435 was created at 10:08:13 UTC, 58 seconds after the red run, and went green through every gate.

**Verified output** (evidence/logs/q2-green-run.log):

```text
✓ main ci · 45798477435
✓ gate 3 - gitleaks (full history) in 15s (ID 139835169199)
✓ gate 1 - lint and unit tests in 13s (ID 139835169464)
✓ gate 2 - integration tests (PostgreSQL) in 32s (ID 139835169648)
✓ build and push arm64 image in 32s (ID 139835484035)
✓ deploy gate - smoke test pushed image in 15s (ID 139835818340)

INF 12 commits scanned.
INF no leaks found
All checks passed!
27 passed, 4 deselected in 0.90s
tests/test_integration.py::test_ready_against_real_db PASSED             [ 25%]
tests/test_integration.py::test_round_trip_persists_hits PASSED          [ 50%]
tests/test_integration.py::test_duplicate_code_raises_code_taken PASSED  [ 75%]
tests/test_integration.py::test_delete_removes_row PASSED                [100%]
======================= 4 passed, 27 deselected in 0.78s =======================
#18 pushing manifest for ghcr.io/praviiiiin/shortlink-lab4:2.1.0-fbefde2@sha256:09a2e10b...3df0 2.9s done
smoke test passed for ghcr.io/praviiiiin/shortlink-lab4@sha256:09a2e10b4facf4e1203614f7db8227613fb1a2e136fa346a2cd275a4fcdb3df0
```

## Experiment 2: gate timings across the real runs

Job and step timings were read from the GitHub API for the four runs that matter (`gh run view --json jobs`).
Times are UTC, durations come from each job's `startedAt` and `completedAt`.

**Verified output** (evidence/logs/q2-run-timings.log, trimmed to job lines):

```text
== run 45798321003 64fd9be failure 10:07:15-10:07:41 UTC wall=26s "Release 2.1.0: strip whitespace and reject credential URLs"
  job gate 3 - gitleaks (full history): success 10:07:19-10:07:27 (8s)
  job gate 1 - lint and unit tests: failure 10:07:19-10:07:37 (18s)
  job gate 2 - integration tests (PostgreSQL): success 10:07:19-10:07:41 (22s)
  job build and push arm64 image: skipped 10:07:41-10:07:41 (0s)
  job deploy gate - smoke test pushed image: skipped 10:07:41-10:07:41 (0s)
== run 45798477435 fbefde2 success 10:08:13-10:10:47 UTC wall=154s "Restore the 2048-character URL limit dropped in 2.1.0"
  job gate 3 - gitleaks (full history): success 10:08:17-10:08:32 (15s)
  job gate 1 - lint and unit tests: success 10:08:16-10:08:29 (13s)
  job gate 2 - integration tests (PostgreSQL): success 10:08:17-10:08:49 (32s)
  job build and push arm64 image: success 10:08:54-10:09:26 (32s)
  job deploy gate - smoke test pushed image: success 10:10:31-10:10:46 (15s)
== run 45804779011 1b807fa success 10:48:45-10:51:05 UTC wall=140s "Release 2.2.0: short_url in link stats and noindex redirects"
  job gate 2 - integration tests (PostgreSQL): success 10:48:49-10:49:10 (21s)
  job gate 3 - gitleaks (full history): success 10:48:49-10:49:05 (16s)
  job gate 1 - lint and unit tests: success 10:48:49-10:49:08 (19s)
  job build and push arm64 image: success 10:49:15-10:49:43 (28s)
  job deploy gate - smoke test pushed image: success 10:50:49-10:51:05 (16s)
```

| Run | Commit | Result | Wall time | Slowest gate | Build | Wait before deploy gate | Deploy gate |
|---|---|---|---|---|---|---|---|
| 45798321003 | `64fd9be` (2.1.0) | failure at gate 1 | 26 s | gate 2, 22 s | skipped | not reached | skipped |
| 45798477435 | `fbefde2` (2.1.0 fix) | success | 154 s | gate 2, 32 s | 32 s | 65 s | 15 s |
| 45804779011 | `1b807fa` (2.2.0) | success | 140 s | gate 2, 21 s | 28 s | 66 s | 16 s |

Within a gate, the tests themselves are cheap: the pytest steps take 1 to 2 seconds in every run, while runner setup, `pip install` (4 to 6 s) and the PostgreSQL service container start (7 to 9 s) dominate.
The 65 and 66 second gaps between the end of `build` and the start of `deploy-gate` are the one minute environment wait timer.
The red run failed in 26 seconds of wall time, so a developer learns about the regression in well under a minute, while a green run needs about two and a half minutes from push to a smoke-tested image.

The 2.2.0 run also printed the coverage total that the 80 % floor is checked against, and the gitleaks commit count grew with the history:

**Verified output** (evidence/logs/q2-run-timings.log):

```text
TOTAL                     180     29    84%
27 passed, 4 deselected in 0.99s
4 passed, 27 deselected in 0.48s
INF 20 commits scanned.
INF no leaks found
smoke test passed for ghcr.io/praviiiiin/shortlink-lab4@sha256:c7ea09d05ccba694390f4cd0282f31b84c4e48380798784026fe4af6ffd7d700
```

## Results

- Four gates sit in front of every deployment: lint plus 27 unit tests with an 80 % coverage floor (84 % measured), 4 integration tests against PostgreSQL 17, a full-history gitleaks scan, and a smoke test of the pushed image by digest in a protected environment.
- A real regression in release 2.1.0 was blocked by gate 1 in 26 seconds; the image build and the deploy gate were skipped, so the broken code never existed as a deployable artefact.
- The one-line fix went green 58 seconds later, published `2.1.0-fbefde2` (digest `sha256:09a2e10b...3df0`), and that exact image later became the canary in Q7.
- Release 2.2.0 passed all gates in 140 seconds and produced `2.2.0-1b807fa` (digest `sha256:c7ea09d0...d700`), the version promoted by Flagger in Q7.

## Observations

- Running the three gates in parallel keeps the critical path equal to the slowest gate (integration, 21 to 32 s) instead of their sum.
- The deploy gate tests the artefact, not the source.
  Running `image@sha256:<digest>` proves that the bytes in GHCR start, connect to PostgreSQL, redirect (302), refuse an unauthenticated delete (401) and accept an authenticated one (204).
- In the first ever run (45795139678) the deploy job was still called `deploy gate - smoke test pushed image + approval`; it was cancelled after waiting 113 s, and the next commit (`0e831f1`, "Use Node 24 action releases and a timed deploy environment") replaced the approval with the one minute timer, which keeps a pause for aborting a release without blocking an unattended lab run.
- Run 45798477435 carried a warning annotation, `Docker network rm failed with exit code 1`, because the smoke container still held the service network during job cleanup.
  Commit `a163d12` added an `if: always()` step that removes the container, and the 2.2.0 run has no annotations (evidence/logs/q2-run-timings.log).
- Secrets used by the gates (`CI_DB_PASSWORD`, `SMOKE_ADMIN_TOKEN`) are repository secrets, the admin token is mounted into the smoke container as a read-only file, and gitleaks guards against a secret ever landing in history.

# Q3. Blue-Green Deployment with Zero-Downtime Switching

> **Question.** Implement a Blue-Green deployment strategy and demonstrate zero-downtime switching between environments. Analyze the advantages and limitations.

## Design

The blue-green environment is the namespace `sl-bg`, reachable for users as `bluegreen.shortlink.test` through the shared Envoy Gateway on host port 8480.
Flagger runs it from one Canary object with `analysis.iterations` and no `stepWeight`, which is Flagger's blue-green mode:

- **Blue** is the Deployment `shortlink-primary`, which Flagger clones from the Git-declared Deployment and which serves every user.
- **Green** is the Git-declared Deployment `shortlink`; Flagger scales it up only when its pod template changes (a new image tag, a new environment variable or a changed Secret).
- Green first runs with 0% of users for six 15 s iterations.
  It must pass a pre-rollout acceptance test (create a link, follow it, expect 302), and under synthetic load from the Flagger load tester its error rate must stay at or below 1% and its p95 latency at or below 250 ms.
  Both metrics come from ShortLink's own Prometheus counters and are restricted to green pods (Q7 shows the queries).
- When the analysis passes, all users are switched to green in one step, the green pod spec is copied into blue, and users are switched back once blue runs the new version; green is scaled to 0.
- Two failed checks (`threshold: 2`) abort the release: green is scaled to 0 and users never leave blue.

A user-side probe (`scripts/probe.py`, 4 workers, about 67 requests per second) followed a short link through the gateway during every run and recorded, per second, the HTTP status, the `X-ShortLink-Version` header and the `X-Served-By` pod name, which tells blue pods (`shortlink-primary-*`) from green pods.

![Figure: Q3 blue-green flow on the Gateway API: analysis with 0% of users on green, a single weight flip to green, then the primary is updated and users return to it](diagrams/q3-bluegreen.svg)

## Key configuration

The Canary as it ran in runs 2 and 3:

{{file:apps/workloads/bluegreen/canary.yaml}}

Zero downtime also depends on the pods.
The Deployment never removes a ready pod before its replacement is ready, and every pod waits 5 s after it is taken out of the endpoints before gunicorn starts its graceful shutdown, so the gateway stops sending it requests first:

```yaml
# apps/workloads/base/deployment.yaml (excerpt)
  strategy: { rollingUpdate: { maxUnavailable: 0, maxSurge: 1 } }
          readinessProbe:
            httpGet: { path: /readyz, port: http }
            periodSeconds: 3
            failureThreshold: 2
          lifecycle:
            preStop: { sleep: { seconds: 5 } }
```

A release is a one-line Git change, for example `newTag: 2.3.0-a4ce538` in `apps/workloads/bluegreen/kustomization.yaml`, which Flux applies (Q4).

## Run 1: provider `kubernetes`, and why it was not a real switch

The first implementation used Flagger's `kubernetes` provider, which switches traffic by changing the selector of the `shortlink` Service.
Release 2.0.0 to 2.2.0 was pushed at 11:07:31 UTC; because the Flux dependency chain was still settling, I triggered `flux reconcile kustomization apps` at 11:09:05 and started a second probe at 11:09:12 so that one probe covered the whole rollout.

**Verified output** (evidence/logs/q3-run1-watch.log):

```text
11:09:19 +106s Progressing weight=0 failedChecks=0 iterations=0
11:09:48 +135s Progressing weight=0 failedChecks=1 iterations=1
11:11:19 +226s Progressing weight=0 failedChecks=1 iterations=7
11:11:33 +240s Promoting weight=0 failedChecks=1 iterations=7
11:12:04 +271s Succeeded weight=0 failedChecks=1 iterations=0
probe bluegreen.shortlink.test 540s: requests=36297 ok=36297 5xx=0 conn_errors=0 other=0 slow(>0.5s)=5 availability=100.000% seconds_with_errors=0 v2.0.0=16296 v2.2.0=20001
probe bluegreen.shortlink.test 420s: requests=28457 ok=28457 5xx=0 conn_errors=0 other=0 slow(>0.5s)=0 availability=100.000% seconds_with_errors=0 v2.0.0=9705 v2.2.0=18752
seconds with both versions live: 6 11:11:34 - 11:11:39
user responses from green (canary) pods: 0
```

The release had no errors, but a selector watcher showed that the Service never pointed at green:

**Verified output** (evidence/logs/q3-run1-selector.log):

```text
11:07:35 selector=shortlink-primary endpoints=shortlink-primary-85494fc99d-wc85d shortlink-primary-85494fc99d-qrrft
11:11:34 selector=shortlink-primary endpoints=... shortlink-primary-85494fc99d-sqxs8 shortlink-primary-6879f9fc74-kcj4w
11:12:05 selector=shortlink-primary endpoints=shortlink-primary-6879f9fc74-kcj4w shortlink-primary-6879f9fc74-kdd89 shortlink-primary-6879f9fc74-4xhjx shortlink-primary-6879f9fc74-tz8t5
```

With the `kubernetes` provider the apex Service always selects the primary, so the cut-over was a rolling update of blue (ReplicaSet `85494fc99d` replaced by `6879f9fc74`), and for 6 seconds users received both versions.
Green was tested, which is valuable, but it was never the environment users switched to.

![Figure: Q3 run 1: users stayed on primary pods throughout; versions changed during a rolling update](../evidence/charts/q3-run1-kubernetes-provider.png)

The fix (commit "Flip blue-green traffic on the Gateway API instead of the Service") changed the provider to `gatewayapi:v1`, so that Flagger owns the HTTPRoute and switches users by changing the backend weights from 100/0 to 0/100 in a single update.

## Run 2: zero-downtime switch 2.2.0 to 2.3.0

Release 2.3.0 came from CI run 45808928784 and was promoted with a Git commit pushed at 11:20:32 UTC.

**Verified output** (evidence/logs/q3-run2-switch.log):

```text
11:22:33 New revision detected! Scaling up shortlink.sl-bg
11:22:48 Pre-rollout check acceptance-test passed
11:22:48 Advance shortlink.sl-bg canary iteration 1/6
11:23:03 Halt advancement no values found for custom metric: error-rate-percent: no values found
11:23:18 Advance shortlink.sl-bg canary iteration 2/6
11:24:18 Advance shortlink.sl-bg canary iteration 6/6
11:24:33 Routing all traffic to canary
11:24:48 Copying shortlink.sl-bg template spec to shortlink-primary.sl-bg
11:25:03 Routing all traffic to primary
11:25:18 Promotion completed! Scaling down shortlink.sl-bg
shortlink-primary=100 shortlink-canary=0
probe bluegreen.shortlink.test 480s: requests=32417 ok=32417 5xx=0 conn_errors=0 other=0 slow(>0.5s)=0 availability=100.000% seconds_with_errors=0 v2.2.0=16359 v2.3.0=16058
seconds in which both versions answered: 1 [('11:24:33', 'v2.2.0=28', 'v2.3.0=35')]
green pods served users from 11:24:33 to 11:25:03 responses: 2024
```

| Measure | Value |
|---|---|
| User requests during the run | 32,417 |
| Failed requests (5xx, connection errors, slow over 0.5 s) | 0, 0, 0 |
| Availability | 100.000% |
| Green tested in isolation before the switch | 6 iterations, 90 s, 0% of users |
| Seconds in which both versions answered | 1 (the switch second itself) |
| Green served all users | 11:24:33 to 11:25:03 (2,024 responses) while blue was updated |
| Push to release complete | 288 s (Flux 121 s, analysis 120 s, switch and finalise 47 s) |

![Figure: Q3 run 2: blue serves until the weight flip, green serves alone for 30 s, then the updated primary takes over; no errors](../evidence/charts/q3-run2-gateway-flip.png)

![Figure: Grafana during run 2: v2.3.0 receives only load-tester traffic during the analysis, then all users; the weight panel shows the single flip to green and back](../evidence/screenshots/q3-grafana-bluegreen-switch.png)

## Run 3: rollback of a faulty green

To prove the gate, the green build was given an injected fault (`FAULT_ERROR_RATE=0.2`, labelled `2.3.0-fault`), pushed at 11:28:32 UTC.

**Verified output** (evidence/logs/q3-run3-rollback.log):

```text
11:30:33 New revision detected! Scaling up shortlink.sl-bg
11:30:48 Halt shortlink.sl-bg advancement pre-rollout check acceptance-test failed POST http://loadtester.flagger-system/ giving up after 1 attempt(s)
11:31:03 Pre-rollout check acceptance-test passed
11:31:03 Advance shortlink.sl-bg canary iteration 1/6
11:31:18 Halt advancement no values found for custom metric: error-rate-percent: no values found
11:31:33 Rolling back shortlink.sl-bg failed checks threshold reached 2
11:31:33 Canary failed! Scaling down shortlink.sl-bg
probe bluegreen.shortlink.test 360s: requests=23884 ok=23883 5xx=1 conn_errors=0 other=0 slow(>0.5s)=0 availability=99.996% seconds_with_errors=1 v2.3.0=23883
5xx at 11:30:32 served_by_app_pods= 63 ok= 63 (the failed response carried no X-ShortLink-Version header, so it came from the gateway, not from a ShortLink pod)
```

The faulty green was rejected 60 s after it started, and not one user request reached it.
The rollback triggers were the acceptance test (the injected fault failed one of its two requests) and a missing metric sample; the error-rate threshold did not get the chance to fire.
The single failed user request happened at 11:30:32, the second in which Flux applied the commit and Flagger started reconciling the HTTPRoute, and it came from Envoy rather than from a ShortLink pod.

![Figure: Q3 run 3: users stayed on blue for the whole run; one gateway-generated 5xx at the start of reconciliation](../evidence/charts/q3-run3-rollback.png)

## Advantages and limitations

Advantages observed:

- **Instant, whole-population cut-over.** All users moved between versions within one second, and run 2 had no failed request among 32,417.
- **Production-like testing before exposure.** Green ran the real image against the real database for 90 s under load with 0% of users, which is how run 3 rejected a broken build without any user impact.
- **Fast rollback.** Before the flip, rollback is just scaling green to 0; after the flip, blue is still running and the weights can be returned in one update.
- **Version consistency.** Unlike a rolling update (run 1), users do not get a random mix of versions for an extended period.

Limitations and risks:

- **Double capacity.** Blue and green both run during the release (4 instead of 2 pods here); for large services this is the main cost.
- **Shared database.** Both colours use the same PostgreSQL database, so schema changes must be backward compatible (expand, migrate, contract); blue-green does not roll back data.
- **All-or-nothing exposure.** A defect that the synthetic analysis misses reaches 100% of users at once; canary (Q7) limits that blast radius.
- **Provider semantics matter.** Run 1 looked like blue-green in the configuration but was a rolling update in practice; it took a user-side probe and a selector watch to see it.
- **Metric gaps count as failures.** Every run logged one `no values found` check while the new pods' counters were warming up; with `threshold: 2`, a second transient gap would have aborted a good release.
  A longer first interval or a lower-bounded query would make the gate more robust.
- **Session state.** ShortLink is stateless, but sticky sessions or in-memory caches would be lost at the flip.

Rollback triggers in this setup are: a failed pre-rollout acceptance test, error rate above 1% or p95 latency above 250 ms on green, missing metric data, and green not becoming ready within the 300 s progress deadline; any two failed checks abort the release.

# Q4. GitOps Delivery with Flux CD and Automated Reconciliation

> **Question.** Implement GitOps-based delivery where the Git repository is the single source of truth for declarative deployments, and demonstrate automated reconciliation.

## Design

The whole Lab 4 platform is declared in the public repository `Praviiiiin/ensp461-devops-lab4` and applied by Flux CD v2.10.1 running inside the kind cluster `pv-lab4`.
Nothing is deployed with `kubectl apply` or `helm install` by hand.
The only out-of-band steps are creating the cluster and loading one secret, the SOPS age private key, into `flux-system/sops-age`; everything after that comes from Git (`platform/bootstrap.sh`).

Flux works as a pull-based control loop:

- A `GitRepository` source polls `main` every minute over HTTPS and stores a tarball artifact of the commit.
  Its `ignore` rule keeps only `clusters/`, `infrastructure/` and `apps/`, so application source, evidence and report changes never trigger a cluster reconciliation.
- The root `Kustomization` `flux-system` applies `clusters/pv-lab4`, which declares four further Kustomizations chained with `dependsOn`: `infra-controllers` (HelmReleases for Envoy Gateway, Flagger, the Flagger load tester, Prometheus, Grafana and metrics-server), `infra-configs` (the Gateway, Flagger MetricTemplates and the Grafana dashboard), `data` (the PostgreSQL pair) and `apps` (the ShortLink canary and blue-green environments).
- Every layer re-applies its path every 2 minutes with server-side apply, `prune: true` deletes objects that were removed from Git, and `wait: true` makes the next layer wait for health, so CRDs exist before custom resources that use them.
- Secrets are committed encrypted with SOPS and age.
  Only `data` and `stringData` are encrypted (`.sops.yaml`), so names and keys stay reviewable, and the `data` and `apps` Kustomizations decrypt them in the cluster with the `sops-age` key.

Delivery of a new application version is therefore a Git commit: CI (Q2) publishes an image, and a one-line change of `newTag` in `apps/workloads/<env>/kustomization.yaml` is the promotion.
Flux applies the changed Deployment template, and Flagger (Q7) decides how traffic moves to it.
Rolling back a configuration change is `git revert`, which Flux applies the same way.

![Figure: Q4 GitOps loop: Git is polled by the Flux GitRepository, four dependent Kustomizations apply it every 2 minutes, and manual drift is overwritten](diagrams/q4-gitops.svg)

## Key configuration

The source and the root Kustomization that point the cluster at the repository:

{{file:clusters/pv-lab4/flux-system/gotk-sync.yaml}}

The reconciliation layers:

{{file:clusters/pv-lab4/layers.yaml}}

The SOPS creation rule (the public age recipient is in Git, the private key only in the cluster):

{{file:.sops.yaml}}

Promotion is a single declarative line in an overlay; this is the canary environment after the 2.2.0 promotion (commit `757afb0`):

```yaml
# apps/workloads/canary/kustomization.yaml
images:
  - name: ghcr.io/praviiiiin/shortlink-lab4
    newTag: 2.2.0-1b807fa  # promotion = changing this line in Git
```

## Experiment 1: bootstrap from Git

`platform/bootstrap.sh` was run against the fresh cluster.
The first attempt exited with status 1 because the sync objects (`GitRepository`, `Kustomization`) were submitted in the same apply as the CRDs that define them.
The script was changed to apply twice with a `kubectl wait --for condition=established` on the two Flux CRDs in between, and the second attempt passed `flux check`.

**Verified output** (evidence/logs/q4-gitops-bootstrap.log):

```text
# 2026-11-26 15:26:01 IST
resource mapping not found for name: "flux-system" namespace: "flux-system" from "clusters/pv-lab4/flux-system": no matches for kind "Kustomization" in version "kustomize.toolkit.fluxcd.io/v1"
ensure CRDs are installed first
[exit 1]

# 2026-11-26 15:26:08 IST
customresourcedefinition.apiextensions.k8s.io/gitrepositories.source.toolkit.fluxcd.io condition met
customresourcedefinition.apiextensions.k8s.io/kustomizations.kustomize.toolkit.fluxcd.io condition met
kustomization.kustomize.toolkit.fluxcd.io/flux-system serverside-applied
gitrepository.source.toolkit.fluxcd.io/flux-system serverside-applied
✔ Kubernetes 1.37.0 >=1.35.0-0
✔ distribution: flux-v2.10.1
✔ helm-controller: deployment ready
✔ kustomize-controller: deployment ready
✔ notification-controller: deployment ready
✔ source-controller: deployment ready
✔ all checks passed
[exit 0]
```

Eleven minutes later every layer was reconciled from the same commit, all HelmReleases were installed, and Flagger had initialised both ShortLink environments.

**Verified output** (evidence/logs/q4-gitops-state.log):

```text
NAME       	REVISION          	SUSPENDED	READY	MESSAGE
flux-system	main@sha1:a78d0d8d	False    	True 	stored artifact for revision 'main@sha1:a78d0d8d'
NAME             	REVISION          	SUSPENDED	READY	MESSAGE
apps             	main@sha1:a78d0d8d	False    	True 	Applied revision: main@sha1:a78d0d8d
data             	main@sha1:a78d0d8d	False    	True 	Applied revision: main@sha1:a78d0d8d
flux-system      	main@sha1:a78d0d8d	False    	True 	Applied revision: main@sha1:a78d0d8d
infra-configs    	main@sha1:a78d0d8d	False    	True 	Applied revision: main@sha1:a78d0d8d
infra-controllers	main@sha1:a78d0d8d	False    	True 	Applied revision: main@sha1:a78d0d8d
NAMESPACE           	NAME          	REVISION	READY	MESSAGE
envoy-gateway-system	envoy-gateway 	1.10.0  	True 	Helm install succeeded ... chart gateway-helm@1.10.0
flagger-system      	flagger       	1.45.0  	True 	Helm install succeeded ... chart flagger@1.45.0
flagger-system      	loadtester    	0.39.0  	True 	Helm install succeeded ... chart loadtester@0.39.0
monitoring          	grafana       	10.5.15 	True 	Helm install succeeded ... chart grafana@10.5.15
monitoring          	metrics-server	3.14.0  	True 	Helm install succeeded ... chart metrics-server@3.14.0
monitoring          	prometheus    	30.0.1  	True 	Helm upgrade succeeded ... chart prometheus@30.0.1
NAMESPACE   NAME                           STATUS        WEIGHT   LASTTRANSITIONTIME
sl-bg       canary.flagger.app/shortlink   Initialized   0        2026-11-26T10:06:21Z
sl-canary   canary.flagger.app/shortlink   Initialized   0        2026-11-26T10:06:36Z
```

## Experiment 2: drift is reverted without anyone touching Git

At the time of the drills the blue-green environment was exposed by a plain `HTTPRoute` declared in `apps/workloads/bluegreen/httproute.yaml` (as of commit `be738b0`):

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata: { name: shortlink }
spec:
  parentRefs: [{ name: public, namespace: envoy-gateway-system }]
  hostnames: [bluegreen.shortlink.test]
  rules:
    - backendRefs: [{ name: shortlink, port: 80 }]
```

The route was deleted by hand with `kubectl`, simulating an operator mistake, while `scripts/wait-http.sh` polled `http://bluegreen.shortlink.test/healthz` through the Envoy gateway once a second.
The site went to HTTP 404 immediately and came back without any human action when the `apps` Kustomization ran its next 2 minute reconciliation and recreated the object.

**Verified output** (evidence/logs/q4-drift-route.log):

```text
$ kubectl -n sl-bg get httproute shortlink; kubectl -n sl-bg delete httproute shortlink; ./scripts/wait-http.sh bluegreen.shortlink.test /healthz 200; ...
NAME        HOSTNAMES                      AGE
shortlink   ["bluegreen.shortlink.test"]   5s
httproute.gateway.networking.k8s.io "shortlink" deleted from sl-bg namespace
10:10:00 +0s HTTP 404
10:11:56 +116s HTTP 200
back to HTTP 200 after 116s
NAME        HOSTNAMES                      AGE
shortlink   ["bluegreen.shortlink.test"]   1s
2m2s        Normal   Progressing               kustomization/apps   HTTPRoute/sl-bg/shortlink created
```

## Experiment 3: a change and its revert, delivered only through Git

Commit `631990c` added a second hostname, `bg.shortlink.test`, to the same route and was pushed to GitHub.
No command was run against the cluster; the poller simply waited for the new hostname to answer.

**Verified output** (evidence/logs/q4-git-change.log):

```text
631990c Serve the blue-green environment on bg.shortlink.test as well
10:14:56
   c10b928..631990c  main -> main
10:14:59 +0s HTTP 404
10:16:57 +118s HTTP 200
back to HTTP 200 after 118s
NAME	REVISION          	SUSPENDED	READY	MESSAGE
apps	main@sha1:631990cd	False    	True 	Applied revision: main@sha1:631990cd
NAME        HOSTNAMES                                          AGE
shortlink   ["bluegreen.shortlink.test","bg.shortlink.test"]   5m1s
```

The change was then undone with `git revert`, the GitOps equivalent of a rollback.

**Verified output** (evidence/logs/q4-git-revert.log):

```text
be738b0 E Pravin Revert "Serve the blue-green environment on bg.shortlink.test as well"
631990c E Pravin Serve the blue-green environment on bg.shortlink.test as well
10:20:20
10:20:20 +0s HTTP 200
10:22:03 +103s HTTP 404
back to HTTP 404 after 103s
NAME	REVISION          	SUSPENDED	READY	MESSAGE
apps	main@sha1:be738b0d	False    	True 	Applied revision: main@sha1:be738b0d
NAME        HOSTNAMES                      AGE
shortlink   ["bluegreen.shortlink.test"]   10m
```

The route object kept its age (5m1s, then 10m) through both commits, which shows that Flux patched the existing object in place instead of recreating it.

## Experiment 4: application releases delivered by Git commits

The same mechanism delivered every ShortLink release in Q7.
The time from `git push` to Flagger noticing the new Deployment template measures the GitOps delivery latency for application changes.

| Commit | Change in Git | Pushed (UTC) | Flagger "New revision detected" (UTC) | Latency |
|---|---|---|---|---|
| `6cb3532` | canary `newTag` 2.0.0 -> 2.1.0-fbefde2 | 10:22:22 | 10:24:07 (watch: `Progressing`) | 105 s |
| `25b3c35` | canary gets a 20 % fault-injection patch | 10:46:49 | 10:49:18 | 149 s |
| `757afb0` | revert of the fault, canary `newTag` 2.2.0-1b807fa | 10:54:11 | 10:56:18 | 127 s |

Sources: push times in `evidence/data/q7-canary-*.pushed-at`, detection times in evidence/logs/q7-canary-interrupted.log, q7-canary-rollback.log and q7-canary-promote.log.

A read-only check at the end of the session shows Git and the cluster in agreement: the remote `main` commit is the revision every Kustomization reports as applied, and both environments run the image tag declared in Git.

**Verified output** (evidence/logs/q4-live-state.log):

```text
a4ce5387139f323937d2d6f892149be75f556a1d	refs/heads/main
NAME       	REVISION          	SUSPENDED	READY	MESSAGE
flux-system	main@sha1:a4ce5387	False    	True 	stored artifact for revision 'main@sha1:a4ce5387'
apps             	main@sha1:a4ce5387	False    	True 	Applied revision: main@sha1:a4ce5387
data             	main@sha1:a4ce5387	False    	True 	Applied revision: main@sha1:a4ce5387
flux-system      	main@sha1:a4ce5387	False    	True 	Applied revision: main@sha1:a4ce5387
infra-configs    	main@sha1:a4ce5387	False    	True 	Applied revision: main@sha1:a4ce5387
infra-controllers	main@sha1:a4ce5387	False    	True 	Applied revision: main@sha1:a4ce5387
33m   Warning  GitOperationFailed  GitRepository/flux-system  failed to checkout and determine revision: unable to list remote for 'https://github.com/Praviiiiin/ensp461-devops-lab4.git': ... context deadline exceeded
33m   Normal   Succeeded           GitRepository/flux-system  stored artifact for commit 'main@sha1:6cb353286077903a98eb35b77c9bd644c361730e'
NAMESPACE   NAME                REPLICAS   IMAGE
sl-canary   shortlink           0          ghcr.io/praviiiiin/shortlink-lab4:2.2.0-1b807fa
sl-canary   shortlink-primary   2          ghcr.io/praviiiiin/shortlink-lab4:2.2.0-1b807fa
sl-bg       shortlink           0          ghcr.io/praviiiiin/shortlink-lab4:2.2.0-1b807fa
sl-bg       shortlink-primary   4          ghcr.io/praviiiiin/shortlink-lab4:2.2.0-1b807fa
```

## Results

| Scenario | Trigger | Measured time to converge |
|---|---|---|
| Manual deletion of a Git-declared HTTPRoute | `kubectl delete` | 116 s (404 back to 200) |
| New hostname added in Git | `git push` of `631990c` | 118 s (404 to 200 on the new host) |
| Change undone with `git revert` | `git push` of `be738b0` | 103 s (200 back to 404) |
| Application releases (three commits) | `git push` | 105 s to 149 s until Flagger saw the new revision |

- The cluster was built from an empty kind node using only the repository and one age key, and all five Kustomizations and six HelmReleases reported `Ready` at the same commit.
- Out-of-band drift was undone automatically, a Git commit and its revert were each delivered with no cluster access, and every application release in Q7 was a commit.

## Observations

- All measured convergence times sit between 103 s and 149 s.
  This is what the configuration predicts: up to 1 minute for the `GitRepository` poll plus up to 2 minutes until the next `apps` interval, so the worst case is about 3 minutes.
  A GitHub webhook to a Flux `Receiver` would cut this to seconds, but the kind cluster is not reachable from the internet, so polling is the honest choice here.
- The 2 minute interval is also the upper bound on how long manual drift can survive.
  Drift detection uses server-side apply, so only fields declared in Git are enforced; fields owned by other controllers, such as replica counts set by the HPA, and objects generated by Flagger (the `-primary` Deployments and the canary HTTPRoute) are left alone.
- Flux recovered from a real network failure on its own.
  Right after the laptop hosting the cluster woke from a 17.5 minute sleep (see Q7), one GitHub fetch timed out (`GitOperationFailed ... context deadline exceeded`).
  The next poll succeeded at the current revision `6cb3532` with no intervention, and later commits were picked up normally.
- Git as the source of truth also bounds runtime decisions.
  When Flagger rolled the faulty canary back in Q7, the cluster served the old version but Git still described the faulty one; the drill was only closed by `git revert` (`b76a9a9`), which brought the desired state and the running state back together.
- The bootstrap failure on the first attempt is a classic ordering problem for any GitOps tool that manages its own CRDs; the two-pass apply with an explicit wait made the script idempotent, and the same ordering is enforced inside the cluster by `dependsOn` and `wait: true`.

# Q5. Advanced Deployment Strategies: Comparison, Risk Analysis and Conclusions

> **Question.** Prepare an advanced deployment strategies report comparing blue-green, canary, GitOps, secret management, and disaster recovery approaches with risk analysis, evidence, and conclusions.

## Scope and method

This section compares the five approaches as I implemented and measured them for ShortLink in Q1 to Q4, Q6 and Q7.
They are not alternatives to each other: GitOps is the delivery channel, blue-green and canary are release strategies on top of it, secret management protects what flows through it, and disaster recovery covers what none of them prevent.
Every figure below is taken from the evidence of the earlier sections; nothing was re-measured for this comparison.
All user-facing numbers come from probes that entered through the same Envoy Gateway as a real client, so they describe what a user would have experienced, not what a controller reported.

## Evidence summary

| Approach | Implementation | Key measured result | Evidence |
|---|---|---|---|
| CI/CD gates (Q2) | GitHub Actions: lint and unit, PostgreSQL integration, gitleaks; arm64 build; smoke-tested deploy gate | A real regression (2048-character URL limit dropped) failed gate 1 and skipped build and deploy; the fix passed every gate | `q2-red-run.log`, `q2-green-run.log` |
| GitOps (Q4) | Flux v2, four dependent Kustomization layers, public Git repository as the only source | Git change live in 118 s, revert in 103 s, deleted HTTPRoute restored in 116 s, releases picked up in 105 s to 149 s | `q4-*.log` |
| Canary (Q7) | Flagger on Gateway API weights, 10% steps to 50%, error-rate and p95 gates | 2.1.0 to 2.2.0: 38,670 requests, 0 errors, observed share within 0.2 points of the weight; faulty canary rolled back at 10% with error rate 18.72%, 94 of 27,250 user requests failed | `q7-*.log` |
| Blue-green (Q3) | Flagger blue-green on Gateway API, 6 isolated iterations, single weight flip | 2.2.0 to 2.3.0: 32,417 requests, 0 errors, both versions in only 1 second; faulty green rejected with 0 user requests reaching it | `q3-*.log` |
| Secret management (Q6) | SOPS with age decrypted by Flux; GitHub Actions secrets; gitleaks | 0 plaintext values in the whole Git history, cluster values equal to the encrypted ones, CI secrets masked as `***` | `q6-secrets.log` |
| Disaster recovery (Q1) | Velero with Kopia to off-cluster Garage S3; PostgreSQL streaming standby with scripted promotion | Restore: RTO 31.7 s, RPO bounded by a 15 min schedule, fingerprint identical; failover: RTO 5.6 s, RPO 0 of 6,995 acknowledged writes | `q1-*.log` |

## Comparison of the release strategies

| Criterion | Rolling update (Q3 run 1) | Blue-green (Q3 run 2) | Canary (Q7 run C) |
|---|---|---|---|
| Users on the new version before it is trusted | gradually, without a gate | 0% during 90 s of isolated tests | 10% to 50% in 30 s steps |
| Blast radius of a bad release | grows with every replaced pod | 0% if caught before the flip, 100% if missed | 10% at the first step (94 failed requests in Q7 run B) |
| Rollback | another rolling update | weight flip back, blue still running | weight to 0 |
| Detection of user-only defects | none | only what synthetic load reveals | real user traffic is part of the analysis |
| Mixed versions | yes (6 s observed) | 1 s at the flip | intentionally, for the whole analysis (about 2.5 min) |
| Extra capacity | surge pods during the roll | a full second set (2 extra pods) | the canary set (2 extra pods) |
| Time from Flagger start to done | 165 s | 167 s | 240 s |
| Failed user requests in the clean release | 0 of 28,457 | 0 of 32,417 | 0 of 38,670 |

The two Flagger strategies need the same infrastructure (gateway, metrics, load tester) and differ mainly in who is exposed while the new version is judged.
Blue-green judges with synthetic traffic and then exposes everyone at once; canary judges with real users and exposes a bounded share first.
The faulty-release drills show the consequence directly: the bad blue-green candidate cost 0 user requests (run 3 in Q3), the bad canary cost 94 (run B in Q7), but the canary's verdict came from real traffic patterns rather than from a load generator.

## Risk analysis

Likelihood and impact are rated 1 (low) to 3 (high); the score is their product.
Mitigations list what this lab already does and, in italics, what a production setup would add.

| # | Risk | Area | L | I | Score | Evidence | Mitigation |
|---|---|---|---|---|---|---|---|
| R1 | Defect that synthetic tests miss reaches all users at the flip | Blue-green | 2 | 3 | 6 | Q3: analysis uses load-tester traffic only | Acceptance webhook plus metric gates; *combine with a short canary phase or mirror real traffic to green* |
| R2 | Bad canary hurts the users routed to it before rollback | Canary | 3 | 1 | 3 | Q7 run B: 94 of 27,250 requests failed in 60 s at 10% | Small first step, 1 min windows, threshold 2; *smaller first step (1 to 5%) and faster interval for high-traffic services* |
| R3 | Metric gap aborts a good release or hides a bad one | Canary, blue-green | 2 | 2 | 4 | `no values found` in every Q3 run; Q7 uses `100 - success%` so 0 errors still returns data | Load tester guarantees traffic; *warm-up interval, alert on repeated gaps* |
| R4 | Incompatible schema change breaks the old version during a mixed-version window | Canary, blue-green | 2 | 3 | 6 | Both colours share one PostgreSQL database (Q3, Q7) | Additive schema only (`CREATE TABLE IF NOT EXISTS`); *expand and contract migrations, migration gate in CI* |
| R5 | Manual hotfix in the cluster is silently reverted | GitOps | 3 | 1 | 3 | Q4: deleted HTTPRoute restored in 116 s | Treat as intended; *document the suspend procedure and use it in incidents (Q1)* |
| R6 | GitOps fights an imperative recovery or applies a stale revision | GitOps, DR | 2 | 3 | 6 | Q1: old primary revived as a standalone primary after `flux resume` | Fencing by Service selector, suspend during incidents; *runbook: commit, reconcile source, then resume* |
| R7 | Git or GitHub unavailable | GitOps | 1 | 2 | 2 | Delivery depends on the public repository | Cluster keeps running the last applied state; *mirror repository, cached artifacts* |
| R8 | age private key leaked or lost | Secrets | 1 | 3 | 3 | Q6: key only in `.secrets/` and `flux-system/sops-age` | Git-ignored, never in backups; *per-environment keys, offline escrow copy, `sops updatekeys` rotation* |
| R9 | Secret committed in plaintext | Secrets | 2 | 3 | 6 | Q6: gitleaks found 0 leaks in 27 commits | gitleaks CI gate on full history; *pre-commit hook, push protection* |
| R10 | Backup unusable when needed | DR | 2 | 3 | 6 | Q1: `dr-drill-2` restore reported `Completed` with no data | Fingerprint verification after each restore; *scheduled automatic restore tests* |
| R11 | Writes lost at failover under load | DR | 2 | 2 | 4 | Q1: RPO 0 at 48 writes/s with 0 bytes lag | Async standby, fast promotion; *synchronous replication for critical data, WAL archiving* |
| R12 | Backups expose decrypted secrets | DR, secrets | 2 | 2 | 4 | Q1: `pg-credentials` included in the Velero backup | Off-cluster bucket with its own credentials; *exclude Secrets or encrypt the bucket* |
| R13 | Single-node platform hides infrastructure failures | All | 3 | 2 | 6 | One kind node; node or zone loss could not be simulated | Drills simulate pod and process failure; *multi-node, multi-zone cluster for a real environment* |

The highest scores (6) cluster around shared state and coordination: the shared database during releases (R1, R4), the interplay of GitOps with manual recovery (R6), untested backups (R10) and leaked secrets (R9).
None of the release strategies addresses the database; that is why Q1 exists and why its drills verified data, not just API status.

## Conclusions

1. **GitOps is the foundation, not a strategy.** Every release, rollback and topology change in this lab was a Git commit, which made the history of the cluster auditable and every change revertible in about two minutes. Its weakness is speed and its interaction with incidents: a 1-minute poll plus dependent layers gave 103 s to 149 s of delivery latency, and recovery steps must be sequenced with `flux suspend` and committed afterwards.
2. **Canary is the safer default for user-facing changes.** It caught a faulty release at 10% exposure using real traffic and promoted a good one with no errors and a traffic split that matched the configured weights within 0.2 points. Its costs are a longer release (240 s here) and a small number of real failed requests when a release is bad.
3. **Blue-green fits changes that must be switched atomically or tested in isolation.** It switched all users within one second with zero failed requests and rejected a broken build before any user saw it, at the cost of double capacity and an all-or-nothing exposure if the synthetic tests are not representative. The tooling detail matters: the same Flagger feature with the `kubernetes` provider produced a rolling update, which only the user-side probe revealed.
4. **Secrets are best managed in the same flow as the manifests.** SOPS with age kept Git free of plaintext while letting Flux deliver secrets with the same review and rollback as code. The trade-off against a vault server is that the age key is a single root of trust and that rotation requires a commit; for a team or for dynamic database credentials a vault would be the next step.
5. **Disaster recovery must be measured and verified.** Streaming replication gave RPO 0 and RTO 5.6 s for the common failure; Velero to off-cluster storage gave a 31.7 s restore with a 15-minute RPO for the rare one. A restore that reports success can still be empty, so each backup path needs an automatic data check.

The combination used here, CI gates, then a Git commit, then Flagger canary or blue-green with metric gates, with SOPS secrets and two layers of recovery, kept every measured clean release at 100% availability while catching both injected faults automatically.

# Q6. Secret Management for the Pipeline and Deployments

> **Question.** Implement secret management for the pipeline and deployments using a secure vault/secret store, and demonstrate safe handling of credentials.

## Design

ShortLink needs five credentials: the PostgreSQL superuser, replication and application passwords, the admin token that guards `DELETE /api/links/<code>`, and the database password used by CI.
They are handled by two stores, chosen so that no plaintext value is ever written to Git or to a log.

- **Deployment secrets: SOPS with age, decrypted by Flux.**
  Secrets live in Git as `*.sops.yaml` files in which only the `data`/`stringData` values are encrypted with AES256-GCM; the data key is wrapped for one age recipient.
  The age private key exists in exactly two places: the git-ignored `.secrets/age.agekey` on the student laptop and the Kubernetes Secret `flux-system/sops-age`, created once by `platform/bootstrap.sh`.
  Flux's kustomize-controller decrypts in memory while applying the `data` and `apps` layers, so the cluster receives ordinary Secrets and Git only ever sees ciphertext.
- **Pipeline secrets: GitHub Actions encrypted secrets.**
  `CI_DB_PASSWORD` (integration and smoke-test database) and `SMOKE_ADMIN_TOKEN` (admin API smoke test) are repository secrets; GHCR pushes use the short-lived `GITHUB_TOKEN` with `packages: write` scoped to the build job only.
  GitHub masks every secret value as `***` in job logs.
- **Leak prevention.**
  gitleaks scans the entire Git history as CI gate 3 on every run, and the same scan runs locally before each push.

A vault server (HashiCorp Vault or OpenBao) was deliberately not used.
SOPS keeps the secret next to the manifest that consumes it, so a secret change goes through the same review, history and rollback (`git revert`) as any other change, and there is no extra stateful service to back up or unseal.
The cost is that rotation needs a commit and that the age key becomes the single root of trust; both points are analysed in Q5.

![Figure: Q6 secret flow: SOPS-encrypted files in Git are decrypted only by Flux inside the cluster; pipeline secrets stay in GitHub Actions](diagrams/q6-secrets.svg)

## Key configuration

The creation rule encrypts only the values of Kubernetes Secrets, so reviewers can still see which keys changed:

```yaml
# .sops.yaml
creation_rules:
  - path_regex: .*\.sops\.yaml$
    encrypted_regex: ^(data|stringData)$
    age: age15pmkew2azjs05juq9g9l5gz7e9fjh4jysjy9aucescntzkyeqp5qef9uw5
```

Flux decrypts with the in-cluster key on the two layers that carry secrets:

```yaml
# clusters/pv-lab4/layers.yaml (apps layer, the data layer is identical)
  decryption: { provider: sops, secretRef: { name: sops-age } }
```

The workload consumes the secrets in the least exposed way that still works.
The database password is an environment variable from a `secretKeyRef` (libpq reads `PGPASSWORD`).
The admin token is mounted as a read-only file with mode `0440`, readable through `fsGroup: 10001` by the non-root app user only, and is re-read on every request and compared with `hmac.compare_digest`, so a rotated value takes effect without a restart and is never part of the pod spec or environment:

```yaml
# apps/workloads/base/deployment.yaml (excerpt)
      automountServiceAccountToken: false
      securityContext: { runAsNonRoot: true, fsGroup: 10001, seccompProfile: { type: RuntimeDefault } }
          env:
            - name: PGPASSWORD
              valueFrom: { secretKeyRef: { name: shortlink-secrets, key: pg-password } }
          volumeMounts:
            - { name: admin-token, mountPath: /etc/shortlink, readOnly: true }
      volumes:
        - name: admin-token
          secret: { secretName: shortlink-secrets, items: [{ key: admin-token, path: admin-token }], defaultMode: 0440 }
```

```python
# app/shortlink/app.py (excerpt)
def admin_ok() -> bool:
    try:
        with open(ADMIN_TOKEN_FILE) as f:
            expected = f.read().strip()
    except OSError:
        return False  # no token configured: admin API is closed
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())
```

In the pipeline the secrets reach only the steps that need them, as environment variables; the smoke test writes the token to a file under `$RUNNER_TEMP` with `umask 077` before mounting it read-only into the container (see the full workflow in Q2).

## Verified results

### 1. Git holds only ciphertext

**Verified output** (evidence/logs/q6-secrets.log):

```text
apiVersion: v1
kind: Secret
metadata:
    name: shortlink-secrets
type: Opaque
stringData:
    pg-password: ENC[AES256_GCM,data:scBcHcsNSXdkDq4VCA3zK36pqvCGuVY4KiMzR2BE8D4Ygq1GPlVf7A==,iv:...,tag:...,type:str]
    admin-token: ENC[AES256_GCM,data:jKTj4bQPrd1zWKNJddnrQMVEX/I6UWZt9TkyX3zJx/thH0Ec...,iv:...,tag:...,type:str]
sops:
    age:
        - enc: |
            -----BEGIN AGE ENCRYPTED FILE-----
.gitignore:1:.secrets/	.secrets/age.agekey
```

The private key file is covered by `.gitignore`, and only the public recipient appears in the repository.

### 2. Flux decrypts inside the cluster, and the values are exactly the encrypted ones

`scripts/secret-audit.sh` reads the plaintext values from the git-ignored `.secrets/` folder, searches the complete patch history of every commit for them, and compares SHA-256 prefixes of the in-cluster values with the local ones; it never prints a value.

**Verified output** (evidence/logs/q6-secrets.log):

```text
NAME   DECRYPTION   KEY
apps   sops         sops-age
data   sops         sops-age
sops-age keys: ['age.agekey']
NAMESPACE   NAME                TYPE     DATA   AGE
sl-bg       shortlink-secrets   Opaque   2      69m
sl-canary   shortlink-secrets   Opaque   2      69m
sl-data     pg-credentials      Opaque   3      70m
== plaintext secret values found anywhere in Git history (all commits, all files):
0
== cluster value vs local plaintext (sha256 prefixes)
sl-data/pg-credentials[app-password] cluster=e4e59c37e894 local=e4e59c37e894 MATCH
sl-canary/shortlink-secrets[pg-password] cluster=e4e59c37e894 local=e4e59c37e894 MATCH
sl-canary/shortlink-secrets[admin-token] cluster=6b695a7c5161 local=6b695a7c5161 MATCH
sl-bg/shortlink-secrets[admin-token] cluster=6b695a7c5161 local=6b695a7c5161 MATCH
```

### 3. Safe handling in the running pod

**Verified output** (evidence/logs/q6-secrets.log):

```text
pod shortlink-primary-c4775dc56-4jmvm
PGDATABASE=sl_canary/
PGHOST=pg.sl-data.svc.cluster.local/
PGUSER=shortlink/
PGCONNECT_TIMEOUT=3/
PGPASSWORD=shortlink-secrets-primary/pg-password
automountServiceAccountToken=false
total 4
-r--r----- 1 root app 64 Nov 26 10:59 admin-token
uid=10001(app) gid=10001(app) groups=10001(app)
command terminated with exit code 2
no
no
DELETE with none token -> 401
DELETE with wrong token -> 401
DELETE with right token -> 204
```

The pod spec only references the secret (`shortlink-secrets-primary/pg-password`); the value is not in the manifest.
Flagger copies `shortlink-secrets` to `shortlink-secrets-primary` when it promotes a release, so a secret change is rolled out through the same canary analysis as code.
There is no service-account token in the container (the `ls` of the token directory fails with exit code 2), and `kubectl auth can-i` answers `no` for the app's service account reading Secrets in its own namespace or listing them in `sl-data`.
The admin API rejects a missing or wrong token with 401 and accepts the mounted one with 204, which shows the file-based token works end to end through the gateway.

### 4. Pipeline secrets and history scan

**Verified output** (evidence/logs/q6-secrets.log):

```text
4:46PM INF 27 commits scanned.
4:46PM INF no leaks found
CI_DB_PASSWORD	2026-11-26T09:46:35Z
SMOKE_ADMIN_TOKEN	2026-11-26T09:46:36Z
{"branch_policy":{"custom_branch_policies":true,"protected_branches":false},"name":"lab4-cluster","protection_rules":["wait_timer","branch_policy"],"wait_timer":1}
  ADMIN_TOKEN: ***
  PGPASSWORD: ***
... -e "POSTGRES_USER=shortlink" -e "POSTGRES_DB=shortlink" -e "POSTGRES_PASSWORD=***" ... postgres:17-alpine
```

The log of CI run 45804779011 shows every use of the two repository secrets masked as `***`, including the service container's `POSTGRES_PASSWORD`.
The same gitleaks scan is CI gate 3 (Q2), so a secret pushed by mistake would block the image build.

## Observations

- Velero backs up Kubernetes Secrets as ordinary manifests (Q1), so the backup bucket holds the decrypted `pg-credentials`; the bucket therefore needs the same protection as the cluster, or Secrets should be excluded from backups and recreated from Git by Flux.
- The age key is the root of trust: losing it makes every `*.sops.yaml` unreadable, and leaking it exposes all of them.
  It is kept outside Git and outside the backup bucket; in a team it would be held per environment and rotated with `sops updatekeys`.
- Rotation is a commit: `sops apps/workloads/canary/shortlink-secrets.sops.yaml`, push, and Flux plus Flagger roll it out with the normal analysis; the file-mounted admin token is then picked up without restarting gunicorn.
- An earlier design idea, a reviewer-approved deploy environment, was replaced by a timed environment because the only possible reviewer is the author (Q2); in a team the `lab4-cluster` environment would require a second person.

# Q7. Canary Deployment with Flagger: Rollout Metrics and Rollback Triggers

> **Question.** Implement a Canary deployment strategy, gradually roll out a new version to a subset of users, and analyze rollout metrics and rollback triggers.

## Design

The canary environment serves ShortLink to users at `canary.shortlink.test` through Envoy Gateway (Gateway API v1).
Flagger 1.45.0 owns the rollout; Flux (Q4) only declares the desired version.

- The Git-declared Deployment `shortlink` is the release under test.
  Flagger clones it into `shortlink-primary`, which serves the stable version, and generates the Services `shortlink`, `shortlink-primary` and `shortlink-canary` plus an `HTTPRoute` for `canary.shortlink.test`.
- When a commit changes the Deployment template (normally the image tag), Flagger scales the canary up and starts an analysis loop every 30 seconds.
  Each successful iteration raises the canary's `backendRefs` weight in the HTTPRoute by 10 points, up to 50 %.
  After a successful iteration at 50 %, Flagger copies the canary template into the primary, routes all traffic back to the (now upgraded) primary, and scales the canary to zero.
- Every iteration evaluates two Prometheus queries computed from the application's own counters, restricted to the canary pods and to user-facing routes: `error-rate-percent` must be at most 1 and `p95-latency-ms` at most 250, both over a 1 minute window.
- A pre-rollout webhook runs an acceptance test (create a link, expect a 302 redirect) against the canary Service before any user sees it, and a rollout webhook runs `hey` against the canary (2 workers at 5 requests per second each) so that metrics exist even at 10 % weight.
- Two failed checks (`threshold: 2`) roll the canary back: weight 0, canary scaled to 0, phase `Failed`, primary untouched.

Users never see a mixed version inside one request; each request is routed by Envoy to one backend according to the current weights.
Prometheus scrapes every pod every 5 seconds (`scrape_interval: 5s`), which keeps the 1 minute analysis window well populated.

![Figure: Q7 canary design: Envoy splits user traffic by Flagger-written weights, Flagger judges every 30 s step with Prometheus queries and webhooks, and two failed checks roll back](diagrams/q7-canary.svg)

## Key configuration

The Canary resource:

{{file:apps/workloads/canary/canary.yaml}}

The two MetricTemplates used by the analysis:

{{file:infrastructure/configs/metric-templates.yaml}}

The fault used for the rollback drill is part of the application and is switched on only through environment variables; the probe endpoints stay healthy on purpose, so readiness probes cannot catch it and only the metrics can:

```python
# app/shortlink/app.py (create_app, before_request)
if request.path in PROBE_PATHS or time.monotonic() - started < after_s:
    return None
if error_rate and random.random() < error_rate:
    return jsonify(error="injected fault"), 500
```

## Method

Three rollouts were run, each started only by a Git commit (Q4).
During every rollout `scripts/probe.py` played the users: 4 workers followed one short link through the gateway (`Host: canary.shortlink.test`) without following redirects, and recorded per second the status code, `X-ShortLink-Version` and `X-Served-By` (a pod name containing `-primary-` means the primary served it).
`scripts/watch-canary.sh` recorded every change of phase, weight and failed-check count of the Canary, and the Flagger controller log, Prometheus range queries and Grafana were read afterwards.
All times are UTC.

| Run | Commit | Change | Outcome |
|---|---|---|---|
| A | `6cb3532` | 2.0.0 -> 2.1.0-fbefde2 | promoted, but interrupted by a host sleep |
| B | `25b3c35` | 2.1.0 with `FAULT_ERROR_RATE=0.2` and `APP_VERSION=2.1.0-fault` | rolled back automatically |
| C | `b76a9a9` + `757afb0` | revert of B, then 2.1.0 -> 2.2.0-1b807fa | promoted cleanly |

## Run C: progressive promotion of 2.2.0

**Verified output** (evidence/logs/q7-canary-promote.log):

```text
pushed at 10:54:11 UTC
10:54:16 +0s Failed weight=0 failedChecks=0 iterations=0
10:56:20 +124s Progressing weight=0 failedChecks=0 iterations=0
10:56:48 +152s Progressing weight=10 failedChecks=0 iterations=0
10:57:19 +183s Progressing weight=20 failedChecks=0 iterations=0
10:57:48 +212s Progressing weight=30 failedChecks=0 iterations=0
10:58:19 +243s Progressing weight=40 failedChecks=0 iterations=0
10:58:48 +272s Progressing weight=50 failedChecks=0 iterations=0
10:59:19 +303s Promoting weight=50 failedChecks=0 iterations=0
10:59:50 +334s Finalising weight=0 failedChecks=0 iterations=0
11:00:19 +363s Succeeded weight=0 failedChecks=0 iterations=0

10:56:18 New revision detected! Scaling up shortlink.sl-canary
10:56:48 Starting canary analysis for shortlink.sl-canary
10:56:48 Pre-rollout check acceptance-test passed
10:56:48 Advance shortlink.sl-canary canary weight 10
10:57:18 Advance shortlink.sl-canary canary weight 20
10:57:48 Advance shortlink.sl-canary canary weight 30
10:58:18 Advance shortlink.sl-canary canary weight 40
10:58:48 Advance shortlink.sl-canary canary weight 50
10:59:18 Copying shortlink.sl-canary template spec to shortlink-primary.sl-canary
10:59:48 Routing all traffic to primary
11:00:18 Promotion completed! Scaling down shortlink.sl-canary
NAME                REPLICAS   IMAGE
shortlink           0          ghcr.io/praviiiiin/shortlink-lab4:2.2.0-1b807fa
shortlink-primary   3          ghcr.io/praviiiiin/shortlink-lab4:2.2.0-1b807fa
probe canary.shortlink.test 600s: requests=38670 ok=38670 5xx=0 conn_errors=0 other=0 slow(>0.5s)=0 availability=100.000% seconds_with_errors=0 v2.1.0=17036 v2.2.0=21634
```

The Canary started from `Failed`, the state left behind by run B, and Flagger treated the new template as a fresh revision.
The share of user requests actually answered by canary pods was computed from the probe CSV for each weight step, leaving a 3 second margin after every weight change:

**Verified output** (evidence/logs/q7-observed-share.log):

```text
promote weight 10 seconds 160 - 186 canary=169 primary=1512 share=10.05% canary_5xx=0
promote weight 20 seconds 190 - 216 canary=336 primary=1342 share=20.02% canary_5xx=0
promote weight 30 seconds 220 - 246 canary=505 primary=1180 share=29.97% canary_5xx=0
promote weight 40 seconds 250 - 276 canary=677 primary=1018 share=39.94% canary_5xx=0
promote weight 50 seconds 280 - 306 canary=846 primary=845 share=50.03% canary_5xx=0
promote weight 50 (Promoting) seconds 310 - 336 canary=843 primary=837 share=50.18% canary_5xx=0
promote weight 0 seconds 342 - 600 canary=0 primary=16536 share=0.00% canary_5xx=0
```

![Figure: Run C, 2.1.0 to 2.2.0: Flagger weight (blue) against the share of probe requests served by canary pods (orange) and answered by v2.2.0 (dotted); no 5xx at any step (evidence/data/q7-canary-promote.csv, q7-canary-promote-weights.json)](../evidence/charts/q7-canary-promote.png)

![Figure: Grafana "ShortLink rollouts" dashboard for run C: requests per second, 5xx rate and p95 latency by version, and Flagger weight per workload](../evidence/screenshots/q7-grafana-canary-promote.png)

## Run B: automatic rollback of a faulty canary

Commit `25b3c35` patched the canary Deployment so that 20 % of user requests fail with HTTP 500, and relabelled the build `2.1.0-fault` so that it is distinguishable in metrics.

**Verified output** (evidence/logs/q7-canary-rollback.log):

```text
pushed at 10:46:49 UTC
10:48:38 +0s Succeeded weight=0 failedChecks=0 iterations=0
10:49:19 +41s Progressing weight=0 failedChecks=0 iterations=0
10:49:50 +72s Progressing weight=0 failedChecks=1 iterations=0
10:50:19 +101s Progressing weight=10 failedChecks=1 iterations=0
10:50:50 +132s Progressing weight=10 failedChecks=2 iterations=0
10:51:18 +160s Failed weight=0 failedChecks=0 iterations=0

10:49:18 info New revision detected! Scaling up shortlink.sl-canary
10:49:48 info Starting canary analysis for shortlink.sl-canary
10:49:48 info Halt shortlink.sl-canary advancement pre-rollout check acceptance-test failed POST http://loadtester.flagger-system/ giving up after 1 attempt(s)
10:50:18 info Starting canary analysis for shortlink.sl-canary
10:50:18 info Pre-rollout check acceptance-test passed
10:50:18 info Advance shortlink.sl-canary canary weight 10
10:50:48 info Halt shortlink.sl-canary advancement error-rate-percent 18.72 > 1
10:51:18 info Rolling back shortlink.sl-canary failed checks threshold reached 2
10:51:18 info Canary failed! Scaling down shortlink.sl-canary
NAME                REPLICAS   IMAGE
shortlink           0          ghcr.io/praviiiiin/shortlink-lab4:2.1.0-fbefde2
shortlink-primary   2          ghcr.io/praviiiiin/shortlink-lab4:2.1.0-fbefde2
```

**Verified output** (evidence/data/q7-canary-rollback.summary and evidence/logs/q7-observed-share.log):

```text
probe canary.shortlink.test 420s: requests=27250 ok=27156 5xx=94 conn_errors=0 other=0 slow(>0.5s)=0 availability=99.655% seconds_with_errors=49 v2.1.0=26859 v2.1.0-fault=389
rollback weight 10 seconds 212 - 268 canary=363 primary=3275 share=9.98% canary_5xx=86
```

![Figure: Run B, rollback drill: the faulty canary received about 10 % of users for one minute, every 5xx came from canary pods, and Flagger returned the weight to 0 (evidence/data/q7-canary-rollback.csv, q7-canary-rollback-weights.json)](../evidence/charts/q7-canary-rollback.png)

![Figure: Grafana for run B: v2.1.0-fault reaches a 5xx rate between about 15 % and 34 % while v2.1.0 stays at 0 %, and the canary weight drops back to 0 after one minute at 10 %](../evidence/screenshots/q7-grafana-canary-rollback.png)

## Run A: a rollout that survived a host sleep

Run A promoted 2.1.0-fbefde2 over 2.0.0, but the laptop hosting the kind cluster went into idle sleep at weight 30 %.
Flagger keeps its progress in the Canary status, so after the wake it simply continued (the first status read after the wake already showed 50 %) and completed the promotion.

**Verified output** (evidence/logs/q7-canary-interrupted.log):

```text
10:22:28 +0s Initialized weight=0 failed=0 iter=0
10:24:07 +99s Progressing weight=0 failed=0 iter=0
10:24:38 +130s Progressing weight=10 failed=0 iter=0
10:25:07 +159s Progressing weight=20 failed=0 iter=0
10:25:38 +190s Progressing weight=30 failed=0 iter=0
host slept 15:55:40-16:13:10 IST (pmset log), resuming watch
10:43:31 +0s Progressing weight=50 failed=0
10:43:49 +18s Promoting weight=50 failed=0
10:44:18 +47s Finalising weight=0 failed=0
10:44:49 +78s Succeeded weight=0 failed=0
```

Its probe (evidence/data/q7-canary-interrupted.summary) recorded 13761 requests with 1 5xx and 3 requests slower than 0.5 s (availability 99.993 %).
Because the probe itself was frozen during the sleep, run A is kept as a robustness observation and not used for the metric analysis below.

## Rollout metric analysis

Flagger publishes the value of each metric it evaluated as `flagger_canary_metric_analysis`.
Reading that gauge back from Prometheus shows exactly what the analysis saw in runs B and C:

**Verified output** (evidence/logs/q7-metric-analysis.log):

```text
== rollback window 10:46:49-10:53:48 UTC: flagger_canary_metric_analysis (value Flagger last evaluated, printed when it changes)
10:46:49 error-rate-percent -0.0
10:50:54 error-rate-percent 18.72
10:46:49 p95-latency-ms 15.06
== promote window 10:54:11-11:04:10 UTC: flagger_canary_metric_analysis (value Flagger last evaluated, printed when it changes)
10:54:11 error-rate-percent 18.72
10:57:21 error-rate-percent 0.0
10:54:11 p95-latency-ms 15.06
10:57:21 p95-latency-ms 9.33
10:57:51 p95-latency-ms 9.64
10:58:21 p95-latency-ms 9.68
10:58:51 p95-latency-ms 9.64
10:59:21 p95-latency-ms 9.44
```

The same MetricTemplate queries were then evaluated as range queries (namespace `sl-canary`, target `shortlink`, interval 1 m, 10 second step) to see the signal between Flagger's checks:

**Verified output** (evidence/logs/q7-metric-analysis.log):

```text
== rollback: error-rate-percent MetricTemplate query (namespace=sl-canary, target=shortlink, interval=1m), 10 s step, canary pods only
10:50:29=34.21 10:50:39=17.68 10:50:49=18.64 10:50:59=20.56 10:51:09=21.69 10:51:19=20.72 10:51:29=19.87 10:51:39=21.01 10:51:49=20.22 10:51:59=17.54 10:52:09=14.45
== promote: error-rate-percent MetricTemplate query (namespace=sl-canary, target=shortlink, interval=1m), 10 s step, canary pods only
10:57:01=0.0 10:57:11=0.0 ... 11:00:41=0.0   (23 samples, all 0.0)
== promote: p95-latency-ms MetricTemplate query (namespace=sl-canary, target=shortlink, interval=1m), 10 s step, canary pods only
10:57:01=9.37 10:57:11=9.17 10:57:21=9.27 10:57:31=9.55 10:57:41=9.64 10:57:51=9.67 10:58:01=9.69 10:58:11=9.7 10:58:21=9.74 10:58:31=9.75 ... 11:00:41=9.54
```

![Figure: Flagger analysis metrics against their thresholds. Left, run B: canary error rate 14 % to 34 % against the 1 % limit, with the two failed checks and the rollback marked. Right, run C: canary p95 latency 9.2 ms to 9.8 ms against the 250 ms limit and an error rate of 0 at every weight step (evidence/logs/q7-metric-analysis.log)](../evidence/charts/q7-analysis-metrics.png)

| Metric | Threshold | Run C (2.2.0) | Run B (2.1.0-fault) |
|---|---|---|---|
| error-rate-percent (canary, 1 m) | max 1 | 0.0 at all 5 checks, 0.0 in all 23 range samples | 18.72 at the failing check, 14.45 to 34.21 in range samples |
| p95-latency-ms (canary, 1 m) | max 250 | 9.33 to 9.68 at the checks, 9.17 to 9.75 in range samples | not evaluated after the error-rate failure |
| user 5xx seen by the probe | none allowed | 0 of 38670 | 94 of 27250 (92 from canary pods) |
| observed canary share vs weight | equal | within 0.2 points at every step | 9.98 % at weight 10 |
| time at each weight | 30 s | 30 s per step, 10 to 50 % | 60 s at 10 %, then 0 |
| detection of new revision to end state | | 240 s to `Succeeded` | 120 s to `Failed` |

## Rollback triggers

The configuration defines the following ways in which a canary is aborted; each one is either demonstrated or directly visible in the evidence.

1. **Error-rate gate.** `error-rate-percent > 1` over the last minute of canary traffic counts as a failed check.
   Run B failed this check with 18.72.
2. **Latency gate.** `p95-latency-ms > 250` counts as a failed check.
   It was never breached; 2.2.0 stayed below 10 ms, so there is a margin of more than 25 times.
3. **Webhook failures.** A failed pre-rollout acceptance test halts the analysis and counts towards the same threshold.
   In run B the first check failed this way at 10:49:48, which is consistent with the fault: the acceptance test makes two requests to the canary, each with a 20 % chance of a 500.
   The rollout `synthetic-load` webhook is a trigger of the same kind if the load tester is unreachable.
4. **Failed-check threshold.** With `threshold: 2` the second failed check rolls back.
   In run B the webhook failure (check 1) and the error-rate failure (check 2) were 60 seconds apart, and the rollback followed 30 seconds after the second check, at the next iteration.
5. **Progress deadline.** If the canary Deployment does not become ready within `progressDeadlineSeconds: 300`, for example when an image cannot be pulled or the pod crash-loops, Flagger rolls back without waiting for metrics.
6. **Git revert.** The human trigger in a GitOps setup is a revert commit.
   Flagger's rollback only restores traffic; it does not change Git, so after run B the repository still declared the faulty build until `b76a9a9` reverted it.

## Results

- Release 2.2.0 reached 10, 20, 30, 40 and 50 % of users in 30 second steps, passed every check, and was promoted 240 seconds after Flagger detected it, with 0 failed requests out of 38670 user requests (availability 100 %).
- The weights Flagger wrote were honoured by Envoy almost exactly: the observed canary share was 10.05, 20.02, 29.97, 39.94 and 50.03 % at the five steps.
- A build failing 20 % of requests was stopped at the first traffic step.
  It was exposed to 9.98 % of users for 60 seconds, served 389 of 27250 probe requests (1.43 %), and the overall availability seen by users stayed at 99.655 %, whereas a full rollout of that build would by construction fail about 20 % of all requests.
- The rollback needed no human action; the primary kept serving `2.1.0-fbefde2` on 2 replicas and the canary was scaled to 0.

## Observations

- The exposure of a bad release is bounded by configuration: at most `threshold x interval` = 2 x 30 s at the current weight, and since the first step is 10 %, the worst case is about one minute of 10 % of users.
  Raising `threshold` makes the rollout more tolerant of a flaky acceptance test but lengthens that exposure.
- The canary measured a 23.69 % error rate in the probe (86 of 363 requests at 10 %), close to the injected 20 %.
  The first range sample of 34.21 % comes from a short window with few requests, which is one reason a single check is not enough evidence and why the threshold counts checks rather than samples.
- Flagger stops at the first failing metric: in run B the `p95-latency-ms` gauge kept its old value of 15.06 through the whole window, so latency was never evaluated for the faulty build.
- The error-rate query is written as `100 - 100 * success / total` so that it returns 0 instead of "no data" when there are no 5xx; the side effect is floating-point noise such as `-1.4e-14`, visible as `-0.0` in the gauge.
  The Grafana panel rounds the same expression to 0.01 (`79464d2`).
- The analysis queries match canary pods with `pod=~"shortlink-[0-9a-zA-Z]+(-[0-9a-zA-Z]+)"`, which cannot match `shortlink-primary-...`, and exclude `/healthz`, `/readyz` and `/metrics`; otherwise the healthy primary and the probe traffic would dilute the canary's error rate below 1 %.
- Two 5xx in run B (seconds 291 and 317 of evidence/data/q7-canary-rollback.csv, after the rollback) carried no `X-ShortLink-Version` header, so they were produced by the gateway rather than by either version of the application; the cause was not isolated.
- During `Promoting` the weight was still 50 %, but both pod sets already ran 2.2.0 because the primary had been updated first; the dotted line in the run C chart reaches 100 % before the canary weight drops to 0, so users never moved back to the old version.
- Run A showed that progress lives in the cluster, not in the controller process: after 17.5 minutes of host sleep Flagger continued the analysis from the Canary status and finished the promotion without any intervention.

#!/usr/bin/env bash
# Q1 drill A: Velero backup of the data tier to the off-cluster Garage bucket, then a restore
# into the fresh namespace sl-data-dr, verified row-for-row against the pre-backup fingerprint.
set -euo pipefail
cd "$(dirname "$0")/.."
name=${1:-dr-drill-$(date +%H%M)}
primary=$(kubectl -n sl-data get svc pg -o jsonpath='{.spec.selector.statefulset\.kubernetes\.io/pod-name}')
FP="select count(*) || ' rows, md5 ' || md5(string_agg(code || '|' || url, ',' order by code)) from links"
ts() { date -u +%T; }
now() { python3 -c 'import time; print(f"{time.time():.1f}")'; }

echo "== $(ts) primary is $primary; seeding 5000 links into sl_canary"
./scripts/pgq.sh sl-data "$primary" sl_canary "insert into links (code, url) select 'dr' || lpad(g::text, 5, '0'), 'https://krmu.edu.in/dr/' || g from generate_series(1, 5000) g on conflict do nothing"
before=$(./scripts/pgq.sh sl-data "$primary" sl_canary "$FP")
before_bg=$(./scripts/pgq.sh sl-data "$primary" sl_bg "$FP")
echo "fingerprint before backup: sl_canary $before | sl_bg $before_bg"

echo "== $(ts) backup $name (sl-data, PVCs excluded: the logical dump carries the data)"
t0=$(now)
velero backup create "$name" --include-namespaces sl-data --exclude-resources persistentvolumeclaims,persistentvolumes --wait
t1=$(now)
phase=$(kubectl -n velero get backup "$name" -o jsonpath='{.status.phase}')
[ "$phase" = Completed ] || { echo "backup phase $phase - aborting drill"; exit 1; }
velero backup describe "$name" --details | grep -E '^Phase|Hooks Attempted|Hooks Failed|Total items|Items backed up|pg-(a|b)-0: dumps|Completed|kopia' || true
velero backup logs "$name" | grep -E 'msg="(running exec hook|stdout: )' | sed -E 's/time="[^"]*" level=info //; s/ backup=.*hookContainer=([a-z]+).*hookPhase=([a-z]+).*/ container=\1 phase=\2/; s/ backup=.*//' | head -4 || true

echo "== $(ts) 250 writes after the backup (they are outside its recovery point)"
./scripts/pgq.sh sl-data "$primary" sl_canary "insert into links (code, url) select 'late-${name##*-}-' || lpad(g::text, 4, '0'), 'https://krmu.edu.in/late/' || g from generate_series(1, 250) g"

echo "== $(ts) restore into fresh namespace sl-data-dr"
t2=$(now)
velero restore create "$name-to-dr" --from-backup "$name" --namespace-mappings sl-data:sl-data-dr --wait
t3=$(now)
velero restore describe "$name-to-dr" | grep -E '^Phase|Total items|Items restored|HooksAttempted|HooksFailed|Hooks' || true
dr_primary=$primary
until got=$(./scripts/pgq.sh sl-data-dr "$dr_primary" sl_canary "$FP" 2>/dev/null) && [ "$got" = "$before" ]; do sleep 1; done
t4=$(now)
got_bg=$(./scripts/pgq.sh sl-data-dr "$dr_primary" sl_bg "$FP")
now_src=$(./scripts/pgq.sh sl-data "$primary" sl_canary "select count(*) from links")
echo "fingerprint restored:      sl_canary $got | sl_bg $got_bg"
[ "$got_bg" = "$before_bg" ] && echo "sl_bg matches"
echo "source now has $now_src rows; restored copy lacks the $(( now_src - ${before%% *} )) post-backup rows (RPO = time since backup)"
kubectl -n sl-data-dr get pods,svc
python3 - "$t0" "$t1" "$t2" "$t3" "$t4" <<'PY'
import sys
t0, t1, t2, t3, t4 = map(float, sys.argv[1:])
print(f"backup took {t1 - t0:.1f}s; restore API call {t3 - t2:.1f}s; restore-to-verified-data (RTO) {t4 - t2:.1f}s")
PY

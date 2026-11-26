#!/usr/bin/env bash
# Q1 drill B: crash the PostgreSQL primary under write load and let pg-failover.sh promote the
# standby. RTO is read from the write probe (first acknowledged write after the crash),
# RPO from acknowledged writes that are missing on the promoted standby.
set -uo pipefail
cd "$(dirname "$0")/.."
out=evidence/data/q1-failover
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
ts() { python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%H:%M:%S.%f")[:-3])'; }

echo "$(ts) freeze GitOps for the data tier during the incident (else Flux would undo the failover)"
flux suspend kustomization data
python3 scripts/write-probe.py --host canary.shortlink.test --seconds 150 --prefix fo --out $out > $out.summary 2>&1 &
probe=$!
./scripts/pg-failover.sh pg-a pg-b > evidence/logs/q1-failover-controller.log 2>&1 &
ctl=$!
sleep 40
echo "$(ts) replication before the crash:"
./scripts/pgq.sh sl-data pg-a-0 postgres "select application_name, state, sync_state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) as replay_lag_bytes from pg_stat_replication"
cid=$(docker exec pv-lab4-control-plane crictl ps -q --name postgres --label io.kubernetes.pod.name=pg-a-0 --label io.kubernetes.pod.namespace=sl-data)
pid=$(docker exec pv-lab4-control-plane crictl inspect -o go-template --template '{{.info.pid}}' "$cid")
crash=$(now)
echo "$(ts) CRASH: SIGKILL to the pg-a-0 postmaster (host pid $pid), node treated as lost (statefulset scaled to 0)"
docker exec pv-lab4-control-plane kill -9 "$pid"
kubectl -n sl-data scale statefulset pg-a --replicas=0
wait $ctl
echo "--- failover controller log"; cat evidence/logs/q1-failover-controller.log
wait $probe
cat $out.summary
python3 scripts/failover-analysis.py $out.csv "$crash"
echo "$(ts) acknowledged writes missing on the promoted primary (RPO):"
python3 -c 'import sys; codes=[l.strip() for l in open(sys.argv[1]) if l.strip()]; print("select count(*) as acked, count(l.code) as present, count(*) - count(l.code) as lost from (values " + ",".join(f"(\x27{c}\x27)" for c in codes) + ") a(code) left join links l using (code);")' $out.acked \
  | kubectl -n sl-data exec -i pg-b-0 -c postgres -- psql -U postgres -d sl_canary -f -
./scripts/pgq.sh sl-data pg-b-0 postgres "select pg_is_in_recovery() as in_recovery, now()"
kubectl -n sl-data get pods -o wide; kubectl -n sl-data get svc pg -o wide

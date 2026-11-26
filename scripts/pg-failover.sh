#!/usr/bin/env bash
# Q1 drill B: automatic database failover controller (runs from the operator machine).
# Health-checks the primary from the standby once a second; after 3 consecutive failures it
# fences the old primary (scale to 0), promotes the standby with pg_promote() and repoints the
# "pg" Service at it. Flux must be suspended for sl-data first, or it would undo the selector.
set -uo pipefail
ns=sl-data primary=${1:-pg-a} standby=${2:-pg-b} fails=0
ts() { python3 -c 'import datetime as d; print(d.datetime.now(d.timezone.utc).strftime("%H:%M:%S.%f")[:-3])'; }
echo "$(ts) watching $primary-0 from $standby-0"
while :; do
  if kubectl -n $ns exec $standby-0 -c postgres -- pg_isready -q -t 1 -h $primary-0.pg-hl -U postgres 2>/dev/null; then
    fails=0
  else
    fails=$((fails + 1)); echo "$(ts) primary check failed ($fails/3)"
  fi
  [ $fails -ge 3 ] && break
  sleep 1
done
echo "$(ts) fencing: scale statefulset/$primary to 0"
kubectl -n $ns scale statefulset $primary --replicas=0
echo "$(ts) promoting $standby-0: $(kubectl -n $ns exec $standby-0 -c postgres -- psql -U postgres -Atc 'select pg_promote(true, 30)')"
kubectl -n $ns patch service pg -p "{\"spec\":{\"selector\":{\"statefulset.kubernetes.io/pod-name\":\"$standby-0\"}}}"
echo "$(ts) service pg now selects $standby-0; in recovery: $(kubectl -n $ns exec $standby-0 -c postgres -- psql -U postgres -Atc 'select pg_is_in_recovery()')"

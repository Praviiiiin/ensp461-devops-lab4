#!/usr/bin/env bash
# wait-http.sh <host> <path> <wanted-status> : polls the gateway once a second, prints every
# status change, and reports how long it took until <host><path> answered <wanted-status>
# after first answering something else (so the outage itself is captured).
host=$1 path=$2 want=$3 start=$(date +%s) last="" left=0
while :; do
  code=$(curl -s -o /dev/null -m 2 -w '%{http_code}' -H "Host: $host" "http://localhost:8480$path")
  [ "$code" != "$last" ] && echo "$(date -u +%T) +$(( $(date +%s) - start ))s HTTP $code" && last=$code
  [ "$code" != "$want" ] && left=1
  [ "$code" = "$want" ] && [ $left = 1 ] && break
  [ $left = 0 ] && [ $(( $(date +%s) - start )) -gt 30 ] && { echo "never left HTTP $want"; exit 1; }
  sleep 1
done
echo "back to HTTP $want after $(( $(date +%s) - start ))s"

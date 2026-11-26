#!/usr/bin/env bash
# watch-canary.sh <namespace> [timeout-seconds]: prints every Flagger phase/weight change of
# Canary "shortlink"; stops once a rollout has started and reached Succeeded or Failed.
ns=$1 limit=${2:-900} start=$(date +%s) last="" started=0
while :; do
  s=$(kubectl -n "$ns" get canary shortlink -o jsonpath='{.status.phase} weight={.status.canaryWeight} failedChecks={.status.failedChecks} iterations={.status.iterations}')
  [ "$s" != "$last" ] && echo "$(date -u +%T) +$(( $(date +%s) - start ))s $s" && last=$s
  case $s in
    Progressing*|Promoting*|Finalising*|Waiting*) started=1 ;;
    Succeeded*|Failed*) [ $started = 1 ] && break ;;
  esac
  [ $(( $(date +%s) - start )) -gt "$limit" ] && { echo timeout; break; }
  sleep 2
done

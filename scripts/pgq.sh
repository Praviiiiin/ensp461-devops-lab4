#!/usr/bin/env bash
# pgq.sh <namespace> <pod> <database> <sql>: run SQL as postgres inside a Postgres member.
kubectl -n "$1" exec "$2" -c postgres -- psql -U postgres -d "$3" -Atc "$4"

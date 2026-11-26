#!/usr/bin/env bash
# prom-range.sh <promql> <start-epoch> <end-epoch> <out.json>: saves a 5s-step range query.
curl -s -G http://localhost:8491/api/v1/query_range --data-urlencode "query=$1" \
  --data-urlencode "start=$2" --data-urlencode "end=$3" --data-urlencode step=5 > "$4"
python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(sys.argv[1], d['status'], len(d['data']['result']), 'series')" "$4"

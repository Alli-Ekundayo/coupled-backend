#!/usr/bin/env bash
# Full flow against a running API (default http://localhost:8000)
B=${1:-http://localhost:8000}; P=${2:-demo}
curl -s $B/health; echo
curl -s -X POST $B/api/v1/parts/search -H 'content-type: application/json' -d '{"query":"3.3V low noise LDO","filters":{"v_in_min":5,"in_stock":true}}' | head -c 400; echo
curl -s -X POST $B/api/v1/projects/$P/netlist -H 'content-type: application/json' -d '{"prompt":"USB-C to 3.3V BME280 sensor board"}' | head -c 300; echo
curl -s -X POST $B/api/v1/projects/$P/place -F netlist_id=$P -F board_width_mm=40 -F board_height_mm=30 | head -c 300; echo
JOB=$(curl -s -X POST $B/api/v1/projects/$P/route -H 'content-type: application/json' -d '{"max_steps":300}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
curl -N $B/api/v1/jobs/$JOB/stream | grep -E '^(event|data)' | cut -c1-160
curl -s "$B/api/v1/projects/$P/export?format=svg" -o $P.svg && echo wrote $P.svg

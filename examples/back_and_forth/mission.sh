#!/usr/bin/env bash
# Loads and flies the back-and-forth mission on a service started by runner.py.
# The frame is omitted, so the drone uses its own position as the origin --
# fine for a single drone, never for a fleet.
set -euo pipefail

BASE="http://localhost:6000"

curl -sf -X POST "$BASE/mission/load" -H 'Content-Type: application/json' -d '{
  "protocol": "protocol:BackAndForthProtocol",
  "node_ip_dict": {"0": "localhost:5000"},
  "initial_position": [0, 0, 2]
}'
echo

curl -sf -X POST "$BASE/mission/setup"
echo
curl -sf -X POST "$BASE/mission/start"
echo
echo "Mission running. Watch it with: curl $BASE/mission/status"
echo "Stop and return to launch with: curl -X POST $BASE/mission/stop"

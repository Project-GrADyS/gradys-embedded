#!/usr/bin/env bash
# Loads and flies the simple-collection mission for the UAV started by
# runner.py. Every node in the fleet must be given the SAME node_ip_dict and
# frame; only initial_position differs per drone.
set -euo pipefail

BASE="http://localhost:6000"

curl -sf -X POST "$BASE/mission/load" -H 'Content-Type: application/json' -d '{
  "protocol": "protocol:SimpleUAVProtocol",
  "node_ip_dict": {
    "1": "localhost:5000",
    "2": "localhost:5001",
    "3": "localhost:5002",
    "4": "localhost:5003",
    "5": "localhost:5004"
  },
  "origin_gps_coordinates": [-15.840081, -47.926642, -0.016],
  "initial_position": [0, 0, 20]
}'
echo

curl -sf -X POST "$BASE/mission/setup"
echo
curl -sf -X POST "$BASE/mission/start"
echo
echo "Mission running. Watch it with: curl $BASE/mission/status"

#!/usr/bin/env bash
# Loads and flies the follow mission on both drones (leader_runner.py on :6000,
# follower_runner.py on :6001). Fleet discipline: everyone loads, then everyone
# sets up, then everyone starts -- so every drone is listening before any of
# them begins sending. The frame is omitted (each drone uses its own boot
# position), which only works because the follower tracks the leader's
# broadcast positions rather than absolute coordinates.
set -euo pipefail

LEADER="http://localhost:6000"
FOLLOWER="http://localhost:6001"
PEERS='{"0": "localhost:5000", "1": "localhost:5001"}'

curl -sf -X POST "$LEADER/mission/load" -H 'Content-Type: application/json' -d '{
  "protocol": "protocol:LeaderProtocol",
  "node_ip_dict": '"$PEERS"',
  "initial_position": [0, 0, 4]
}'
echo
curl -sf -X POST "$FOLLOWER/mission/load" -H 'Content-Type: application/json' -d '{
  "protocol": "protocol:FollowerProtocol",
  "node_ip_dict": '"$PEERS"',
  "initial_position": [-2, -2, 2]
}'
echo

curl -sf -X POST "$LEADER/mission/setup"
echo
curl -sf -X POST "$FOLLOWER/mission/setup"
echo

curl -sf -X POST "$LEADER/mission/start"
echo
curl -sf -X POST "$FOLLOWER/mission/start"
echo
echo "Mission running. Status: curl $LEADER/mission/status ; curl $FOLLOWER/mission/status"

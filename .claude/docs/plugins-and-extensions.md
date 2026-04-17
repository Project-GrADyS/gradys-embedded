# Plugins (and Why Extensions Don't Exist Here)

Source: `gradys_embedded/protocol/plugin/`. The dispatcher pattern and the built-in plugins mirror `gradys-sim-nextgen` almost line-for-line — intentional, so the same plugin-using protocol runs in both environments without change.

**Extensions (`gradysim.simulator.extension.*` — Radio, Camera) do not exist in gradys-embedded.** They are simulator-only helpers that read `PythonProvider` directly. Under `EmbeddedProvider` they would silently no-op — see `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/plugins-and-extensions.md` which covers the extension no-op behavior. If a protocol instantiates `Radio(self)` or `Camera(self)` and runs here, the extension's methods will produce no effect on hardware.

## The dispatcher

`gradys_embedded/protocol/plugin/dispatcher.py` — identical contract to the simulator. `create_dispatcher(protocol)` monkey-patches `handle_timer`, `handle_telemetry`, `handle_packet`, `initialize`, and `finish` into call chains. Handlers return `DispatchReturn.CONTINUE` or `DispatchReturn.INTERRUPT`.

```python
from gradys_embedded.protocol.plugin.dispatcher import create_dispatcher, DispatchReturn

class MyProtocol(IProtocol):
    def initialize(self):
        dispatcher = create_dispatcher(self)
        dispatcher.register_handle_telemetry(self._observe)

    def _observe(self, _instance, telemetry):
        # ...
        return DispatchReturn.CONTINUE
```

Idempotent per-protocol: calling `create_dispatcher` twice returns the same dispatcher, so multiple plugins co-exist.

## Built-in plugins

### MissionMobilityPlugin (`plugin/mission_mobility.py`)

Flies a list of cartesian NEU waypoints. Configuration: `speed`, `loop_mission` (`LoopMission.NO` / `RESTART` / `REVERSE`), `tolerance`. Exposes `start_mission(waypoints)`, `stop_mission()`, `current_waypoint`, `is_reversed`, `is_idle`. Hardware behavior:

- Issues `GotoCoordsMobilityCommand` per waypoint — the command translates to `/movement/go_to_gps` via `cartesian_to_geo` (see `→ .claude/docs/mobility-and-telemetry.md` for the mobility path).
- Advances to the next waypoint when telemetry reports the drone within `tolerance` meters.
- Because hardware telemetry is polled at `telemetry_interval` (default 0.5 s), the drone can overshoot a tight tolerance between polls. Raise the tolerance or lower the interval for tight missions.

### RandomMobilityPlugin (`plugin/random_mobility.py`)

Samples random waypoints from configurable `x_range`, `y_range`, `z_range` and flies to each in sequence. Same hardware caveats as `MissionMobilityPlugin`.

### FollowMobilityPlugin (`plugin/follow_mobility.py`)

Two plugins in one module:

- `MobilityLeaderPlugin` — broadcasts its position on a timer. Every broadcast is a real HTTP POST to every peer; see fire-and-forget semantics in `→ .claude/docs/cross-node-communication.md`.
- `MobilityFollowerPlugin` — moves toward the leader's last-reported position with a configurable offset.

Reserved tag names (`FollowMobilityPlugin__leader_broadcast_timer`, `FollowMobilityPlugin__leader`) are used internally — do not reuse. `MobilityLeaderPlugin` does not move the leader and composes with other mobility plugins; `MobilityFollowerPlugin` controls its node's movement exclusively — do not pair with another mobility plugin on the same follower.

### StatisticsPlugin (`plugin/statistics.py`)

Wraps the protocol to collect per-timer statistics. On hardware this **writes to a local pandas DataFrame in the drone's process memory** — there is no central collection. To harvest stats from a fleet, each drone must write to disk in `finish()` and you aggregate after landing. Review the plugin's source before relying on it for hardware runs.

### Raft consensus (`plugin/raft/`)

A full Raft leader-election + log-replication implementation layered on protocol hooks. Files:

- `raft_consensus.py` — plugin class.
- `raft_state.py` — follower/candidate/leader state machine.
- `raft_node.py` — per-node data structures.
- `raft_message.py` — wire format for AppendEntries, RequestVote.
- `failure_detection/` — heartbeat-based failure detector.
- `raft_config.py` — tunable timeouts.

**Raft on hardware is sensitive to timeouts.** Default election and heartbeat timeouts are tuned for the simulator's instantaneous delivery. On a real Wi-Fi mesh with 50–200 ms latency and occasional drops, tighten retries and widen election timeouts to avoid thrashing between candidate states. See `raft_config.py` for the knobs.

## Mobility plugins + direct commands — race warning

Same warning as in the simulator, with teeth here: if your protocol calls `self.provider.send_mobility_command` directly while `MissionMobilityPlugin` is also issuing commands, the commands race on the wire. On hardware, the last POST to `/movement/go_to_gps` wins — the drone executes whichever arrived most recently at `uav_api`, which depends on aiohttp task scheduling order (non-deterministic under load).

Rule: call `stop_mission()` before any manual `send_mobility_command`, and `start_mission()` again after.

## Writing a plugin

Same pattern as the simulator. Minimum viable plugin:

```python
from gradys_embedded.protocol.plugin.dispatcher import create_dispatcher, DispatchReturn

class LoggingPlugin:
    def __init__(self, protocol):
        dispatcher = create_dispatcher(protocol)
        dispatcher.register_handle_packet(self._log_packet)

    def _log_packet(self, _instance, message):
        print(f"packet: {message}")
        return DispatchReturn.CONTINUE
```

Embedded-specific rules on top of the simulator's:

- **Plugins run on the asyncio loop.** Anything slow inside a registered handler blocks the message server, timer dispatch, and telemetry fetch. No threads.
- **Plugin-scheduled timers go through `self.provider.schedule_timer`**, which hits `loop.call_at`. The same-tag-overwrite quirk from `→ .claude/docs/encapsulator-interface.md` applies — cancel before re-scheduling if your plugin reuses a tag.
- **Plugins that broadcast (leader, heartbeat, Raft) produce O(n) HTTP per tick.** Budget network capacity accordingly.

## Related docs

- `→ .claude/docs/protocol-interface.md` — the hook invocation model the dispatcher intercepts.
- `→ .claude/docs/mobility-and-telemetry.md` — where mission/random/follow plugins' commands ultimately land.
- `→ .claude/docs/cross-node-communication.md` — the HTTP that leader broadcasts and Raft messages travel over.
- `→ .claude/docs/encapsulator-interface.md` — timer-scheduling quirks plugins must respect.
- `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/plugins-and-extensions.md` — the same plugin set in the simulator, plus Extensions (Radio, Camera) that do not apply here.

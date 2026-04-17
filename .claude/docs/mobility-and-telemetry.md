# Mobility and Telemetry

How `MobilityCommand` and `Telemetry` cross the boundary between abstract protocol code and the real drone. Sources: `gradys_embedded/protocol/position.py`, `encapsulator/embedded.py` (mobility translation), `runner/runner.py` (telemetry polling).

## The cartesian frame — NEU with a shared origin

Protocols work in meters, using a tuple `(x, y, z)`. The convention is **NEU**:

- `x` = North (positive = north of origin)
- `y` = East (positive = east of origin)
- `z` = Up (positive = altitude above origin altitude)

The origin is `RunnerConfiguration.origin_gps_coordinates = (lat, lon, alt)`, a single GPS point. All protocol code reasons in this frame.

**Every node in the network must use the same origin.** There is no automatic check — if two drones pass different `origin_gps_coordinates`, their cartesian frames disagree silently and waypoints drift apart linearly with the origin offset.

## Conversion helpers

`gradys_embedded/protocol/position.py` exposes both directions:

```python
geo_to_cartesian(ref_coord, target_coord) -> (x, y, z)    # GPS → NEU
cartesian_to_geo(ref_coord, target_coord) -> (lat, lon, alt)  # NEU → GPS
```

- `geo_to_cartesian` uses the haversine formula along latitude and longitude axes separately, then signs `x`/`y` based on whether the target is north/east of the reference. Altitude is a raw difference. Accurate within a few kilometers of the reference.
- `cartesian_to_geo` uses a flat-earth approximation: `dlat = x / 111320`, `dlon = y / (111320 * cos(lat))`. Good enough for small cartesian extents (hundreds of meters); error grows with distance.

These are not exact inverses over long distances. For the scales GrADyS drones fly at (typically <1 km from origin) the round-trip error is sub-meter.

`squared_distance(a, b)` is also exported — use it for waypoint-arrival checks without paying for `sqrt`.

## MobilityCommand → uav_api

`EmbeddedProvider.send_mobility_command` dispatches on `command_type`. All three variants use the fire-and-forget pattern (`_fire_and_forget` schedules an aiohttp call as an asyncio task; failures are logged, the protocol is never notified).

### `GOTO_COORDS` — cartesian input

```python
self.provider.send_mobility_command(GotoCoordsMobilityCommand(50, 0, 20))
```

The provider:

1. Calls `cartesian_to_geo(origin_gps_coordinates, (x, y, z))` → `(lat, lon, alt)`.
2. POSTs `http://localhost:<uav_api_port>/movement/go_to_gps` with `{"lat": lat, "long": lon, "alt": alt, "look_at_target": false}`.

The drone starts moving; the command returns immediately (non-blocking). **Arrival is not acknowledged through any callback** — use `handle_telemetry` to detect when the drone is near the target.

### `GOTO_GEO_COORDS` — GPS input

```python
self.provider.send_mobility_command(GotoGeoCoordsMobilityCommand(lat, lon, alt))
```

Same endpoint (`/movement/go_to_gps/`), no conversion, same fire-and-forget semantics. Use this when your protocol already reasons in GPS (e.g., a target transmitted over the wire with absolute coordinates).

### `SET_SPEED`

```python
self.provider.send_mobility_command(SetSpeedMobilityCommand(5.0))
```

GETs `http://localhost:<uav_api_port>/command/set_air_speed?new_v=<int(speed)>`. **The speed is cast to `int`** — sub-meter/s precision is lost. If you need finer control, extend the provider or use `uav_api` directly for that node.

Persists until changed; subsequent `GOTO_*` commands use the new speed.

## Telemetry polling

`EmbeddedRunner._periodic_telemetry` (see `→ .claude/docs/runtime-model.md` which covers the runner's loop structure) runs forever on the asyncio loop:

1. `GET http://localhost:<uav_api_port>/telemetry/gps` every `telemetry_interval` seconds (default 0.5).
2. Parses `data["info"]["position"]` → `(lat, lon, relative_alt)`. **Altitude is `relative_alt`** — height above takeoff — not `absolute_alt`. This matches the NEU frame where `z=0` means "at origin altitude".
3. Converts with `geo_to_cartesian(origin_gps_coordinates, geo_coords)` → cartesian NEU.
4. Delivers `Telemetry(current_position=cartesian)` to `encapsulator.handle_telemetry`, which forwards to `protocol.handle_telemetry`.

### Failure handling

```python
except Exception as e:
    self._logger.error(f"Telemetry fetch failed: {e}")
```

A fetch failure is logged and the loop sleeps until the next interval. The protocol receives **no signal** that telemetry dropped — `handle_telemetry` simply doesn't fire. If your protocol depends on timely telemetry, timestamp it inside the hook and watch for gaps yourself.

### Tuning the interval

- **Lower `telemetry_interval`** (e.g., 0.1 s) for tight waypoint tolerances or fast-moving platforms. Beware of `uav_api` / MAVLink throughput — a drone link that can't push GPS that fast will return stale or duplicate data.
- **Higher interval** (e.g., 1–2 s) for long-range scenarios where position changes slowly. Saves CPU and network, but delays arrival detection.

The interval is per-node; set it in each drone's `RunnerConfiguration`.

## Full round-trip example

```python
class Surveyor(IProtocol):
    WAYPOINTS = [(0, 0, 20), (100, 0, 20), (100, 100, 20), (0, 100, 20)]

    def initialize(self):
        self._idx = 0
        self.provider.send_mobility_command(
            GotoCoordsMobilityCommand(*self.WAYPOINTS[0])
        )

    def handle_telemetry(self, telemetry):
        target = self.WAYPOINTS[self._idx]
        if squared_distance(telemetry.current_position, target) < 4:  # 2 m tol
            self._idx = (self._idx + 1) % len(self.WAYPOINTS)
            self.provider.send_mobility_command(
                GotoCoordsMobilityCommand(*self.WAYPOINTS[self._idx])
            )
```

Pattern: issue the command, wait for telemetry to report arrival, issue the next. This is also exactly how `MissionMobilityPlugin` works — see `→ .claude/docs/plugins-and-extensions.md` for the reusable version.

## Related docs

- `→ .claude/docs/configuration.md` — `origin_gps_coordinates` invariant, `initial_position`, `telemetry_interval`.
- `→ .claude/docs/runtime-model.md` — when the telemetry loop and setup movement run.
- `→ .claude/docs/encapsulator-interface.md` — the fire-and-forget task mechanism used by mobility commands.
- `→ /home/fleury/gradys/major_projects/uav_api/.claude/docs/specification.md` — authoritative contract for `/movement/go_to_gps`, `/command/set_air_speed`, and `/telemetry/gps`.
- `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/messages-and-telemetry.md` — the abstract shapes (`MobilityCommand`, `Telemetry`, `Position`, `geo_to_cartesian`) that this project implements.

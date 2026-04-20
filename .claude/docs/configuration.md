# Configuration

Every knob in `RunnerConfiguration` and the invariants that must hold across the fleet. Source: `gradys_embedded/runner/configuration.py`.

## The dataclass

```python
@dataclass
class RunnerConfiguration:
    node_id: int
    node_ip_dict: dict[int, str]
    origin_gps_coordinates: tuple[float, float, float]
    initial_position: tuple[float, float, float]
    uav_api_port: int
    telemetry_interval: float = 0.5
```

All fields except `telemetry_interval` are required. There is no validation beyond type hints; the runner will fail at runtime (usually inside the `POST /protocol/setup` handler) if values are incoherent.

## Per-node fields

### `node_id: int`

The node's unique id in the fleet. Must be a key in `node_ip_dict`. Returned by `self.provider.get_id()` inside the protocol.

Convention: small positive integers starting from 1 (or 0). The example in `examples/simple/ge.py` uses `1..5`. There is no hard constraint — any int works as long as every node agrees which id is which.

### `node_ip_dict: dict[int, str]`

Maps every node's id to an `"ip:port"` string (no scheme — `http://` is added automatically when needed). Every node in the fleet must have an entry, including the node itself.

```python
node_ip_dict = {
    1: "192.168.1.10:5000",
    2: "192.168.1.11:5000",
    3: "192.168.1.12:5000",
}
```

The runner extracts its own entry (`node_ip_dict[node_id]`), parses host/port with `rsplit(":", 1)`, and binds the FastAPI message server on `0.0.0.0:<port>`. For peer sends, it reads the full `ip:port` string and POSTs to `http://<ip:port>/message`.

Rules:

- **Every node must have the same `node_ip_dict`.** Drifted copies cause messages to drop silently.
- **Ports can differ per node** — some fleets run every node on `:5000`, others mix ports. Both work.
- **IPs must be reachable from every other node.** Use a dedicated LAN/mesh; do not rely on DHCP.

Full communication details: `→ .claude/docs/cross-node-communication.md` which covers the `/message` payload, SEND/BROADCAST semantics, and fire-and-forget failure modes.

### `initial_position: tuple[float, float, float]`

The cartesian NEU `(x, y, z)` point the drone flies to when `POST /protocol/setup` is handled. The z component is also passed as the takeoff altitude (`GET /command/takeoff?alt=<z>`).

Choose this per-node: each drone needs a different starting point to avoid collisions on the launch pad. Typical pattern:

```python
initial_positions = {
    1: (0, 0, 20),
    2: (10, 0, 20),
    3: (0, 10, 20),
}
```

Expressed in the shared cartesian frame, so offsets are meters from the shared GPS origin.

## Fleet-wide invariants

These **must** be identical across every node's `RunnerConfiguration` or the fleet is broken.

### Shared `origin_gps_coordinates`

The reference `(lat, lon, alt)` that defines the `(0, 0, 0)` point of the cartesian frame. See `→ .claude/docs/mobility-and-telemetry.md` for the NEU convention and conversion helpers.

If node A uses `origin=(−15.840, −47.926, 0)` and node B uses `origin=(−15.841, −47.926, 0)`, their cartesian frames disagree by ~110 m on the x axis. A broadcast `"go to (50, 0, 20)"` means different GPS points for each. **No runtime check enforces origin agreement** — it is an operational invariant, typically managed by keeping a single config file and distributing it to every drone.

### Shared `node_ip_dict`

As above — every node sees every other node via the same map.

## Per-node overrides

These can legitimately differ between nodes:

### `uav_api_port: int`

The port of the local `uav_api` HTTP server. Typically `8000` on every drone, but nothing prevents per-drone overrides (e.g., if you also run a ground-station `uav_api` on a different port on the same host for testing).

### `telemetry_interval: float = 0.5`

Seconds between GPS polls. Default 0.5 s is a reasonable balance between responsiveness and load. Lower it for fast-moving platforms or tight waypoint tolerance; raise it for slow traversals or weak links. Tuning notes in `→ .claude/docs/mobility-and-telemetry.md` which covers the telemetry loop and failure handling.

## Initialization sequence

The aiohttp session is created when `start_api()` schedules `_serve_api` (before uvicorn binds). When `POST /protocol/setup` is then handled, the order is:

1. `GET /command/arm` → 200 required.
2. `GET /command/takeoff?alt=<initial_position[2]>` → 200 required.
3. Compute `cartesian_to_geo(origin_gps_coordinates, initial_position)` → `(lat, lon, alt)`.
4. `POST /movement/go_to_gps_wait` with those coordinates — **blocks until arrival**.

Any step returning non-200 aborts setup; the endpoint returns 500 and `_setup_done` stays False so the operator can retry. Details of the runtime that follows successful setup: `→ .claude/docs/runtime-model.md` which covers bootstrap, the unified API, telemetry loop, and shutdown.

## Launching multiple nodes

There is no fleet launcher; each drone runs its own Python process. A typical deployment:

1. Distribute the same `protocol.py` and `ge.py` skeleton to every drone.
2. On each drone, set `node_id` to a unique value and run `python ge.py`.
3. All other fields (`node_ip_dict`, `origin_gps_coordinates`, `initial_position[:2]` offsets) come from shared config — keep them in a file read at startup rather than hand-coded per drone.

The reference example (`examples/simple/ge.py`) hard-codes configuration; production deployments should parameterize it.

## Common misconfigurations

| Symptom | Likely cause |
|---|---|
| `POST /protocol/setup` returns 500, log says arm failed | `uav_api` not running on `uav_api_port`; drone not GPS-locked; safety switch engaged |
| Drones take off but never reach `initial_position` | `origin_gps_coordinates` does not match the drone's actual starting GPS fix — `go_to_gps_wait` spins forever |
| Messages lost | Drifted `node_ip_dict`, unreachable peers, or both |
| Waypoint arrival never triggers | Protocol's tolerance too tight vs. `telemetry_interval` × drone speed; drone overshoots between polls |
| Two drones collide on takeoff | Same `initial_position` on both |

## Related docs

- `→ .claude/docs/runtime-model.md` — how `start_api`, `/protocol/setup`, and `/protocol/start` sequence, and what each does with the config.
- `→ .claude/docs/mobility-and-telemetry.md` — what `origin_gps_coordinates` and `telemetry_interval` actually control.
- `→ .claude/docs/cross-node-communication.md` — how `node_ip_dict` is used on both the server and client sides.
- `→ /home/fleury/gradys/major_projects/uav_api/.claude/docs/specification.md` — authoritative endpoint spec for the calls `/protocol/setup` makes.

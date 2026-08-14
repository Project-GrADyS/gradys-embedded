# Configuration

Provisioned (machine) vs mission (experiment) configuration, and the invariants that must hold across the fleet. Sources: `gradys_embedded/runner/configuration.py`, `gradys_embedded/runner/config_file.py`, `gradys_embedded/runner/control_panel.py` (`LoadRequest`), `gradys_embedded/runner/mission.py` (`_build_mission_configuration`).

## The split

Two dataclasses with a hard boundary between them:

```python
@dataclass
class RunnerConfiguration:      # provisioned — what a machine needs to boot idle
    node_id: int
    uav_api_port: int
    control_api_port: int
    data_port: int
    certfile: str | None = None
    keyfile: str | None = None
    runs_dir: str = "~/gradys_runs"
    protocols_dir: str = "~/gradys_protocols"
    min_free_disk_mb: int = 200
    telemetry_landed_alt: float = 0.5

@dataclass
class MissionConfiguration:     # mission — carried by POST /mission/load only
    node_ip_dict: dict[int, str] | None = None
    initial_position: tuple[float, float, float] | None = None
    origin_gps_coordinates: tuple[float, float, float] | None = None
    x_axis_degrees: float | None = None
    communication_protocol: str = "http"
    auto_scout: bool = False
    telemetry_interval: float = 0.5
```

`RunnerConfiguration` is what `EmbeddedRunner(configuration)` takes and the **only** thing the TOML loader (`gradys-embedded --config <file.toml>`) accepts. It is bound to the drone and its network — set once at provisioning, stable for as long as the drone sits on a given LAN.

`MissionConfiguration` describes an experiment. It exists so `POST /mission/load` has a validated shape to carry; the mission API is its **only** gateway — it cannot be provisioned, and nothing in it falls back to a provisioned value. There is no default protocol either: every load names its protocol in the request body.

`MissionContext(provisioned, mission)` (frozen, with passthrough properties for every field of both halves) pairs the two after a load. It is what flows to everything downstream — the `EmbeddedProvider`, the communication backend, and the telemetry loop all read a mix of both halves off one object. To amend one (frame resolution does this), a new context is built around a `dataclasses.replace` of the mission half; contexts are never mutated.

## Provisioned fields (`RunnerConfiguration`)

### `node_id: int`

The node's unique id in the fleet. Returned by `self.provider.get_id()` inside the protocol. Convention: small positive integers; any int works as long as every node agrees which id is which. When a mission supplies a `node_ip_dict`, this id should be a key in it (mandatory for zenoh without `auto_scout` — see below).

### `uav_api_port: int`

The port of the local `uav_api` HTTP server. Typically `8000` on every drone; per-drone overrides are fine. Always plain HTTP on `localhost`, regardless of the mission's `communication_protocol`.

### `control_api_port: int`

Dedicated HTTP port for the **control plane** (`/mission/*`, `/protocols/*`, `/runs/*`). This is the port an operator (or gradys-gs) curls to run missions. It is always plain HTTP, independent of the mission's transport, and is bound at boot — an idle drone listens on nothing else. Must be unique per node when several nodes run on one host (the sitl-tester allocates `base_control_port + i`, default base `6000`).

### `data_port: int`

The port this node binds for the inter-node **data plane** (the `/message` server for http/https/http3, or the Zenoh listen endpoint for zenoh transports). **Required** — the peer map is mission-supplied, so nothing else can provide the bind port. The port is a property of the machine; *which transport* serves it is chosen per mission at `POST /mission/load`, and nothing is bound on it until a mission loads. Every other node's `node_ip_dict` entry for this node must point at this port.

### `certfile: str | None` / `keyfile: str | None`

TLS material (PEM), as files on this machine — provisioned, not mission-supplied. Used when a mission selects `"https"`, `"http3"`, or `"zenoh_quic"`; ignored for `"http"` and `"zenoh_tcp"`. `keyfile` is required whenever `certfile` is set.

- **`"https"`/`"http3"`** — server identity, and client-side trust anchor for verifying peers. If omitted, an ephemeral self-signed cert is generated when the backend is built at mission load (temp file, not persisted) and client verification is disabled — encrypted but unauthenticated.
- **`"zenoh_quic"`** — QUIC TLS link identity **and** fleet trust anchor (`listen_certificate`, `listen_private_key`, `root_ca_certificate` all point at it; name verification is disabled so IP-addressed endpoints work). Every node must share the **SAME** cert/key pair, or peers reject each other. Because the ephemeral fallback would make a fleet silently fail to mesh, **`/mission/load` rejects `zenoh_quic` with 400 when no `certfile` is provisioned**. (gradys-sitl-tester auto-generates one shared pair for a local fleet.)

### `runs_dir: str = "~/gradys_runs"` / `protocols_dir: str = "~/gradys_protocols"`

Where run data and uploaded protocol modules live. One subdirectory per run (`run_<timestamp>[_label]/` with the statistics CSVs, `resource_usage.csv`, `runner.log`, `RUN_INFO.json`); `GET /runs` lists it and the download endpoints serve it. `protocols_dir` is what `POST /protocols/upload` writes to and is placed on `sys.path` so protocols import by module name. Both are `expanduser`-ed in `__post_init__`.

### `min_free_disk_mb: int = 200`

Refuse a new run (`POST /mission/load` returns 507) when the `runs_dir` filesystem has less free space than this. Nothing is ever auto-pruned — losing flight data to an automatic prune is worse than a refused start. Set 0 to disable.

### `telemetry_landed_alt: float = 0.5`

Relative altitude (meters) below which the vehicle counts as landed. `uav_api` exposes no flight-mode or armed-state endpoint, so after `/mission/stop` issues RTL the service infers touchdown from the telemetry it already polls; this threshold is what moves the mission from RETURNING back to IDLE.

## Mission fields (`POST /mission/load` body)

The request body (`LoadRequest` in `control_panel.py`) is `protocol` (required, `"module"` or `"module:ClassName"`), an optional `label` (run-directory suffix), plus the `MissionConfiguration` fields below. Omitted fields take the dataclass defaults; validation errors become 400s. `/mission/status` echoes the effective frame, peer map, and transport back so the mission layer can verify every drone agrees **before** starting anything — no code checks agreement across nodes.

### `node_ip_dict: dict[int, str] | None`

Maps node id to `"ip:port"` for the data plane — bare `host:port`, **no scheme** (a scheme is rejected with 400: the send path builds `http://{addr}/message`, so `http://http://...` would fail every send silently). Each entry's port must be that node's provisioned `data_port`; the map no longer supplies any bind port — the node binds `0.0.0.0:<data_port>` regardless.

Requiredness at load:

- **Required (400 without it)** for every transport **except** `zenoh_tcp`/`zenoh_quic` with `auto_scout=true`, which discover peers via multicast instead.
- **Zenoh without `auto_scout` additionally requires the node's own entry** (400 without it) — its IP becomes the Zenoh listen address (`<scheme>/<own_ip>:<data_port>`); the other entries become explicit connect endpoints.

Supplying the map per mission is what lets the fleet gain or lose a drone between missions without re-provisioning every other one. Within a running mission the map is fixed. Full communication details: `→ .claude/docs/cross-node-communication.md`.

### `initial_position: tuple[float, float, float] | None`

The cartesian `(x, y, z)` point the drone flies to when `POST /mission/setup` is handled; z doubles as the takeoff altitude. Not required at load, but **`/mission/setup` returns 400 without it**. Choose per-node — same `initial_position` on two drones means a launch-pad collision. Expressed in the shared cartesian frame (meters from the shared origin).

### `origin_gps_coordinates: tuple[float, float, float] | None` / `x_axis_degrees: float | None`

The shared coordinate frame: the `(lat, lon, alt)` that defines cartesian `(0, 0, 0)`, and the clockwise rotation of the x-axis from true north (`0.0` keeps NEU: x=North, y=East). If either is omitted, `resolve_frame` fills it **from this drone's own telemetry at load time**, with a loud warning — every drone then has a *different* frame, and "go to (50, 0, 20)" means a different place on each. A fleet must be given the same explicit values. If `uav_api` is unreachable during frame resolution, the load fails with 502. See `→ .claude/docs/mobility-and-telemetry.md` for the NEU convention and conversion helpers.

### `communication_protocol: str = "http"`

The inter-node data-plane transport for this mission. Loading binds the data plane for it — and only for it — on `data_port`. One of:

- `"http"` (default) — HTTP/1.1 over TCP via uvicorn, plain (no TLS).
- `"https"` — HTTP/1.1 over TLS via uvicorn, using the provisioned `certfile`/`keyfile` (or an ephemeral self-signed cert).
- `"http3"` — HTTP/3 over QUIC (UDP) via Hypercorn, TLS 1.3; requires `pip install "gradys-embedded[http3]"`.
- `"zenoh_tcp"` — Eclipse Zenoh pub/sub in **peer (p2p) mode** over **TCP** links (no TLS); requires `pip install "gradys-embedded[zenoh]"`. Key-addressed (`gradys/msg/<dest_id>`, `gradys/msg/broadcast`), so BROADCAST is a single publish. Discovery governed by `auto_scout`.
- `"zenoh_quic"` — same Zenoh peer pub/sub over **QUIC** links (TLS 1.3). Needs the fleet-wide provisioned `certfile`/`keyfile` or the load is rejected with 400.

Rejected at load with 400: an unknown value, a transport whose optional extra is not installed on this drone (the error names the `pip install` to run), and `zenoh_quic` without a provisioned cert. **Fleet-wide invariant** — every node's load must name the same transport; mixed transports cannot interoperate. Unrelated to the local `uav_api` connection (always plain HTTP on localhost). A mission on a different transport rebinds the same `data_port`; consecutive missions on the same one reuse the listener. Full transport comparison: `→ .claude/docs/cross-node-communication.md`.

### `auto_scout: bool = False`

Whether nodes discover each other automatically instead of via `node_ip_dict`. **Only implemented for the zenoh transports** — accepted but **ignored** (with a logged warning) for `"http"`/`"https"`/`"http3"`. When zenoh:

- `False` (default) — multicast disabled, explicit `connect.endpoints` built from `node_ip_dict` (works on multicast-blocked LANs; the map stays authoritative and must include this node's own entry).
- `True` — default UDP multicast scouting (`224.0.0.224:7446`); peers auto-discover and `node_ip_dict` is not needed (the one case where load accepts its absence). Requires multicast on the LAN.

### `telemetry_interval: float = 0.5`

Seconds between GPS polls **while this mission is in force**; must be positive (400 otherwise). While no mission is loaded the service polls at its own fixed idle rate (`EmbeddedRunner.IDLE_TELEMETRY_INTERVAL`, 0.5 s) with no frame conversion. Lower it for fast-moving platforms or tight waypoint tolerance. Tuning notes: `→ .claude/docs/mobility-and-telemetry.md`.

## The TOML loader (`runner/config_file.py`)

`gradys-embedded --config <file.toml>` builds a `RunnerConfiguration` and nothing else. The loader is deliberately strict — a fleet's config is generated, so a key that does not apply is a bug in the generator:

- **Mission-level keys are rejected by name** (`node_ip_dict`, `initial_position`, `origin_gps_coordinates`, `x_axis_degrees`, `communication_protocol`, `auto_scout`, `telemetry_interval` — derived from `MissionConfiguration`'s fields) with an error pointing at `POST /mission/load`. They can no longer be provisioned.
- **The removed `protocol` key gets a pointed error**: the default-protocol concept is gone; every load names its protocol.
- **Unknown keys raise**, listing the valid ones.
- **Missing required keys raise** — `node_id`, `uav_api_port`, `control_api_port`, `data_port` have no defaults.

## Fleet-wide invariants

These must be identical across every node's `POST /mission/load` or the fleet is broken. Nothing enforces them across nodes — `/mission/status` echoes them per drone so the mission layer can diff before `/mission/start`.

- **Shared frame** (`origin_gps_coordinates` + `x_axis_degrees`). If node A uses `origin=(−15.840, −47.926, 0)` and node B uses `origin=(−15.841, −47.926, 0)`, their cartesian frames disagree by ~110 m on the x axis, and a broadcast `"go to (50, 0, 20)"` means different GPS points on each. A drone that fell back to its own GPS (frame omitted at load) shows up as a diverging `frame` in its status.
- **Shared `node_ip_dict`** — every node sees every other node via the same map. Drifted copies drop messages silently.
- **Shared `communication_protocol` (and `auto_scout`)** — mixed transports cannot talk.
- **Shared `certfile`/`keyfile`** (provisioned) for `zenoh_quic`, and recommended for peer authentication on `https`/`http3`.

Per-node by design: `node_id`, `initial_position`, and any provisioned field.

## Launching multiple nodes

Each drone runs its own service process, usually `gradys-embedded --config /etc/gradys/embedded.toml`. The provisioned TOML differs between drones only in `node_id` (and ports, when several nodes share a host); it is templated at provisioning time. Everything mission-shaped is fanned out at flight time by the mission layer (gradys-gs): identical `/mission/load` bodies to every drone, differing only in `initial_position`. The service boots idle — a rebooting drone in the field never spontaneously arms.

## Common misconfigurations

| Symptom | Likely cause |
|---|---|
| `POST /mission/load` returns 400 | Missing `node_ip_dict` (non-auto_scout transports), scheme inside a `node_ip_dict` value, unknown transport, missing pip extra, or `zenoh_quic` without a provisioned `certfile` |
| `POST /mission/load` returns 502 | `uav_api` unreachable during frame resolution — it must be up on `uav_api_port` at load, not just at setup |
| `POST /mission/load` returns 500, "Data plane failed to start" | `data_port` already bound by another process, or bad TLS material |
| `POST /mission/load` returns 507 | `runs_dir` filesystem below `min_free_disk_mb` — download and delete old runs |
| `POST /mission/setup` returns 400 | Mission was loaded without `initial_position` |
| `POST /mission/setup` returns 500, log says arm failed | `uav_api` not running on `uav_api_port`; drone not GPS-locked; safety switch engaged (retryable — mission stays LOADED) |
| Drones take off but never reach `initial_position` | `origin_gps_coordinates` does not match the drone's actual starting GPS fix — `go_to_gps_wait` spins forever |
| Messages lost | Drones loaded with drifted `node_ip_dict`s, entries not pointing at each peer's `data_port`, or unreachable peers |
| Waypoint arrival never triggers | Protocol's tolerance too tight vs. `telemetry_interval` × drone speed; drone overshoots between polls |
| Status stuck at `returning` after landing | Telemetry poll broken (landing is inferred from `relative_alt <= telemetry_landed_alt`); `POST /mission/reset` is the escape hatch |
| Two drones collide on takeoff | Same `initial_position` on both |

## Related docs

- `→ .claude/docs/runtime-model.md` — the mission state machine, what load/setup/start/stop/reset each do with this configuration.
- `→ .claude/docs/mobility-and-telemetry.md` — what `origin_gps_coordinates`, `x_axis_degrees`, and `telemetry_interval` actually control.
- `→ .claude/docs/cross-node-communication.md` — how `node_ip_dict` and `data_port` are used on both the server and client sides.
- `→ /home/fleury/Documents/lac/uav_api/.claude/docs/specification.md` — authoritative endpoint spec for the calls `/mission/setup` makes.

# Configuration

Every knob in `RunnerConfiguration` and the invariants that must hold across the fleet. Source: `gradys_embedded/runner/configuration.py`.

## The dataclass

```python
@dataclass
class RunnerConfiguration:
    node_id: int
    node_ip_dict: dict[int, str]
    initial_position: tuple[float, float, float]
    uav_api_port: int
    control_api_port: int
    origin_gps_coordinates: tuple[float, float, float] | None = None
    telemetry_interval: float = 0.5
    communication_protocol: str = "http"
    auto_scout: bool = False
    certfile: str | None = None
    keyfile: str | None = None
```

All fields except `origin_gps_coordinates`, `x_axis_degrees`, `telemetry_interval`, `communication_protocol`, `auto_scout`, `certfile`, and `keyfile` are required. `communication_protocol` is validated against `{"http","https","http3","zenoh_tcp","zenoh_quic"}` in `__post_init__` (invalid values raise `ValueError`); everything else relies on type hints and the runner will fail at runtime (usually inside the `POST /protocol/setup` handler) if values are incoherent.

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

The runner extracts its own entry (`node_ip_dict[node_id]`), parses host/port with `rsplit(":", 1)`, and binds the **data plane** on `0.0.0.0:<port>` — the FastAPI `/message` server for http/https/http3, or the Zenoh transport for `"zenoh_tcp"`/`"zenoh_quic"`. For HTTP peer sends it reads the full `ip:port` and POSTs to `http://<ip:port>/message`; for zenoh under `auto_scout=False` the same `ip:port` entries become Zenoh `tcp/<ip:port>` (`zenoh_tcp`) or `quic/<ip:port>` (`zenoh_quic`) connect endpoints. The control plane (`/protocol/*`) is **not** on this port — it is on `control_api_port`.

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

### `control_api_port: int`

Dedicated HTTP port for the **control plane** (`POST /protocol/setup`, `POST /protocol/start`). This is the port an operator curls to bring the drone up. It is always plain HTTP, independent of `communication_protocol`. The **data plane** (`/message`, or the Zenoh transport) lives separately on this node's `node_ip_dict` port — the two never share a port. Must be unique per node when several nodes run on one host (the sitl-tester allocates `base_control_port + i`, default base `6000`).

### `telemetry_interval: float = 0.5`

Seconds between GPS polls. Default 0.5 s is a reasonable balance between responsiveness and load. Lower it for fast-moving platforms or tight waypoint tolerance; raise it for slow traversals or weak links. Tuning notes in `→ .claude/docs/mobility-and-telemetry.md` which covers the telemetry loop and failure handling.

### `communication_protocol: str = "http"`

Selects the inter-node message-API transport. One of:

- `"http"` (default) — HTTP/1.1 over TCP via uvicorn, plain (no TLS).
- `"https"` — HTTP/1.1 over TLS via uvicorn, using `certfile`/`keyfile` (or an ephemeral self-signed cert).
- `"http3"` — HTTP/3 over QUIC (UDP) via Hypercorn, TLS 1.3; requires the optional deps `pip install "gradys-embedded[http3]"`.
- `"zenoh_tcp"` — Eclipse Zenoh pub/sub in **peer (p2p) mode** over **TCP** links (no TLS); requires `pip install "gradys-embedded[zenoh]"`. Messages are routed by key expression (`gradys/msg/<dest_id>`, `gradys/msg/broadcast`) rather than per-peer IP POSTs, so BROADCAST is a single publish. Discovery is governed by `auto_scout` (below).
- `"zenoh_quic"` — same Zenoh peer pub/sub, but links run over **QUIC** (TLS 1.3). QUIC mandates TLS and verifies peers against a shared root CA, so multi-node operation needs a **fleet-wide shared `certfile`/`keyfile`** (identical on every node) — see below. Full details: `→ .claude/docs/cross-node-communication.md`.

Invalid values raise `ValueError` at construction. **This is a fleet-wide invariant** — every node must use the same value, like `origin_gps_coordinates`; mixed transports cannot interoperate. It does not affect the local `uav_api` connection, which always uses plain HTTP on `localhost`. Full transport comparison: `→ .claude/docs/cross-node-communication.md`.

### `auto_scout: bool = False`

Whether nodes discover each other automatically instead of via the static `node_ip_dict`. **Only implemented for the zenoh transports (`"zenoh_tcp"`, `"zenoh_quic"`)** — it is accepted but **ignored for `"http"`/`"https"`/`"http3"`** (those transports have no discovery; a warning is logged if set). When zenoh:

- `False` (default) — Zenoh peer mode with multicast **disabled** and explicit `connect.endpoints` built from `node_ip_dict` (works on multicast-blocked LANs; keeps `node_ip_dict` authoritative). The node binds its `node_ip_dict` port directly (`tcp/` for `zenoh_tcp`, `quic/` for `zenoh_quic`).
- `True` — Zenoh peer mode using default **UDP multicast scouting** (`224.0.0.224:7446`); peers auto-discover and `node_ip_dict` is not needed for transport. Requires multicast on the LAN; on a single host the default interface may need to be specified.

### `certfile: str | None = None` / `keyfile: str | None = None`

TLS material for `"https"`, `"http3"`, and `"zenoh_quic"` (ignored when `communication_protocol` is `"http"` or `"zenoh_tcp"`):

- **`"https"`/`"http3"`** — the server requires a certificate. If both are provided, the server binds with them and the client verifies peers against `certfile`. If omitted, the server binds with an ephemeral self-signed cert generated at boot (temp file, not persisted) and the client disables verification (`verify=False`) — encrypted but unauthenticated.
- **`"zenoh_quic"`** — the cert is used as the QUIC TLS link identity **and** the fleet trust anchor (set as `listen_certificate`, `listen_private_key`, and `root_ca_certificate`; name verification is disabled so IP-addressed endpoints work). Because QUIC verifies every peer against that root CA, **every node must share the SAME cert/key** — distribute one pair fleet-wide. If omitted, an ephemeral per-node cert is generated and a loud warning is logged: peers on other nodes will **not** trust each other, so multi-node `zenoh_quic` will not communicate. (gradys-sitl-tester auto-generates one shared pair for a local fleet.)

For mutual authentication on `"https"`/`"http3"`, distributing the **same** cert/key to every node is likewise recommended (another fleet-wide invariant).

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
- `→ /home/fleury/Documents/lac/uav_api/.claude/docs/specification.md` — authoritative endpoint spec for the calls `/protocol/setup` makes.

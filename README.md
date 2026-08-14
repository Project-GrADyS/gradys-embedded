# GrADyS-Embedded

GrADyS-Embedded runs [GrADyS-Sim NextGen](https://project-gradys.github.io/gradys-sim-nextgen/) protocols on real quadcopters. It translates the simulator's abstract interfaces into HTTP calls against a local UAV API and inter-node message endpoints, so the same protocol code that works in simulation can fly on actual hardware without modification.

## Motivation

[GrADyS-Sim NextGen](https://project-gradys.github.io/gradys-sim-nextgen/) is a network simulation framework for developing and validating decentralized algorithms in environments populated by nodes capable of communication and movement. Its core design principle is **protocol portability**: protocols interact with their environment exclusively through the `IProtocol` and `IProvider` interfaces, making them independent of any specific execution backend. As stated in its documentation, "you can re-utilize that same code in completely different environments as long as someone has done the work of integrating that environment with the interfaces that the protocol expects."

The simulator supports multiple execution modes (prototype, integrated, and experiment), allowing the exact same protocol logic to run in all of them without changing a line of code. However, the simulator's documentation acknowledges that bridging protocols to real-world deployment is "not ready yet."

GrADyS-Embedded fills this gap. It provides an `IProvider` implementation that maps communication commands to HTTP requests between nodes and mobility commands to UAV API calls, allowing protocols developed and validated in simulation to be deployed directly onto real quadcopters.

## Installation

```bash
pip install gradys-embedded
```

Or install from source:

```bash
git clone https://github.com/Project-GrADyS/gradys-embedded.git
cd gradys-embedded
pip install -e .
```

**Dependencies:** `fastapi`, `uvicorn`, `aiohttp`, `pydantic`, `python-multipart`, `cryptography`, `pandas`.

The `http` and `https` message transports work with the core install. The HTTP/3 (QUIC)
transport (`communication_protocol="http3"`) and the Zenoh transports
(`communication_protocol="zenoh_tcp"` or `"zenoh_quic"`) each need an optional extra:

```bash
pip install "gradys-embedded[http3]"   # adds hypercorn[h3], niquests
pip install "gradys-embedded[zenoh]"   # adds eclipse-zenoh (peer/p2p pub-sub; zenoh_tcp + zenoh_quic)
```

## Quick Start

### 1. Write a protocol

A protocol implements the `IProtocol` interface. The same class works in both simulation and on real hardware.

```python
import json
import logging

from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.communication import BroadcastMessageCommand


class MyProtocol(IProtocol):
    def initialize(self) -> None:
        self.logger = logging.getLogger(__name__)
        self.logger.info(f"Node {self.provider.get_id()} initialized")
        # Schedule a periodic heartbeat
        self.provider.schedule_timer("heartbeat", self.provider.current_time() + 1)

    def handle_timer(self, timer: str) -> None:
        command = BroadcastMessageCommand(json.dumps({"from": self.provider.get_id()}))
        self.provider.send_communication_command(command)
        # Reschedule
        self.provider.schedule_timer("heartbeat", self.provider.current_time() + 1)

    def handle_packet(self, message: str) -> None:
        data = json.loads(message)
        self.logger.info(f"Received message from node {data['from']}")

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        self.logger.info(f"Position: {telemetry.current_position}")

    def finish(self) -> None:
        self.logger.info("Protocol finished")
```

### 2. Start the service

The configuration carries only what is bound to the machine — everything that
describes an experiment arrives later, with the mission:

```python
from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

configuration = RunnerConfiguration(
    node_id=1,
    uav_api_port=8000,
    control_api_port=6000,
    data_port=5000,
)

runner = EmbeddedRunner(configuration)
runner.start_api()
```

Or, more usually on a drone, as a service:

```bash
gradys-embedded --config /etc/gradys/embedded.toml
```

`start_api()` owns the asyncio loop and serves the control plane immediately; the data plane binds when a mission loads:

- **Control plane** (`control_api_port`, always plain HTTP): `/protocols`, `/mission` and `/runs` — see [Mission API](#mission-api).
- **Data plane** (`data_port`, transport chosen per mission at `POST /mission/load`):
  - `POST /message` — inter-node delivery for http/https/http3 (forwarded to `handle_packet`); under `"zenoh_tcp"`/`"zenoh_quic"` a Zenoh peer session carries messages instead.

**The service boots idle and flies nothing until told to.** The mission API is
the only way to run a protocol — a drone that reboots in the field must never
spontaneously arm.

Each node in the network runs its own instance of `EmbeddedRunner` with a unique `node_id`.

### 3. Load and fly a mission

```bash
BASE=http://<drone>:6000

curl -X POST $BASE/mission/load -H 'Content-Type: application/json' -d '{
  "protocol": "my_protocol:MyProtocol",
  "node_ip_dict": {"1": "192.168.1.10:5000", "2": "192.168.1.11:5000"},
  "origin_gps_coordinates": [-15.840081, -47.926642, 0.0],
  "x_axis_degrees": 0.0,
  "initial_position": [0, 0, 20]
}'
curl -X POST $BASE/mission/setup
curl -X POST $BASE/mission/start
```

The `examples/` directories each pair a provisioned-only `runner.py` with a
`mission.sh` showing the full sequence.

## Mission API

The service stays up across missions: load a protocol, fly it, stop it, and load
a different one without restarting anything. Only one mission runs at a time.

```
IDLE ──load──▶ LOADED ──setup──▶ READY ──start──▶ RUNNING ──stop──▶ RETURNING ──▶ IDLE
```

```bash
BASE=http://<drone>:<control_api_port>

curl -F file=@my_protocol.py $BASE/protocols/upload
curl -X POST $BASE/mission/load -H 'Content-Type: application/json' \
     -d '{"protocol": "my_protocol", "label": "sweep-3",
          "node_ip_dict": {"1": "192.168.1.10:5000"},
          "initial_position": [0, 0, 20]}'
curl -X POST $BASE/mission/setup    # arm, take off, fly to initial_position
curl -X POST $BASE/mission/start
curl $BASE/mission/status           # state + live tracked_variables
curl -X POST $BASE/mission/stop     # flush data, then return to launch
```

| Method | Path | Purpose |
|---|---|---|
| POST | `/protocols/upload` | Upload a `.py` protocol module |
| GET | `/protocols` | List available protocols |
| DELETE | `/protocols/{name}` | Remove an uploaded protocol |
| POST | `/mission/load` | Select a protocol and transport, and open a run directory |
| POST | `/mission/setup` | Arm, take off, fly to the initial position |
| POST | `/mission/start` | Instantiate the protocol and begin |
| POST | `/mission/stop` | End the protocol, flush its data, return to launch |
| POST | `/mission/reset` | Operator override: force the state back to idle. Does **not** command the vehicle |
| GET | `/mission/status` | State, run id, elapsed time, live `tracked_variables` |
| GET | `/runs` | List runs held on the drone, with disk usage |
| GET | `/runs/{id}` | Manifest: files, sizes, outcome |
| GET | `/runs/{id}/files/{name}` | Download one file |
| GET | `/runs/{id}/archive` | Download the whole run as `.tar.gz` |
| DELETE | `/runs/{id}` | Delete a run to reclaim space |

`setup` and `start` are separate so a fleet can reach its initial positions
before any protocol begins.

**`POST /mission/load` carries everything experiment-scoped**: the protocol, the
peer map, the coordinate frame, the initial position, the transport and the poll
rate. `node_ip_dict` is required at load (400 without it) unless the transport is
zenoh with `auto_scout=true`, whose multicast scouting discovers peers. The frame
must be identical fleet-wide; `/mission/status` echoes everything back so the
mission layer can verify agreement before starting anything.

**The transport is chosen at load, and only the chosen one is served.** The data
plane binds nothing until a mission loads; a mission on a different transport
rebinds the same port, and consecutive missions on the same transport reuse the
listener. Load rejects a transport the drone cannot serve — unknown name, a
missing optional extra, or `zenoh_quic` without a fleet-wide certificate — and
fails with a 500 if the listener cannot actually bind (port taken, bad TLS
files), rather than returning 200 and failing inside a server nobody is
watching.

`stop` returns immediately: it finishes the protocol and flushes the run first,
then issues RTL fire-and-forget, because `uav_api`'s `/command/rtl` blocks until
the vehicle is home *and* disarmed. Status reports `returning` until the drone
lands. A stopped protocol can no longer command the vehicle or send messages, so
it cannot fight the return.

`reset` is the operator escape hatch: `returning` is normally exited by landing
detection, so if telemetry breaks mid-return the drone would be stuck. Reset
forces any non-idle mission back to idle and finalizes its run — without
commanding the vehicle (an RTL already issued keeps flying).

### Run data

Each mission gets `{runs_dir}/run_<timestamp>[_label]/` containing its statistics
CSVs, `resource_usage.csv`, a per-run `runner.log`, and `RUN_INFO.json` recording
the protocol, coordinate frame and outcome. Data is written periodically as well
as at stop, so an unclean shutdown costs seconds of samples rather than the whole
flight.

Nothing is ever deleted automatically. Below `min_free_disk_mb` the service
refuses to open a new run (HTTP 507) rather than pruning an uncollected flight.

## Configuration Parameters

Configuration is split in two, and the split is structural: `RunnerConfiguration`
(the constructor / the TOML file) carries only what is bound to the machine,
while everything describing an experiment travels in the `POST /mission/load`
body. The TOML loader rejects a mission-level key by name — it cannot be
provisioned.

### Provisioned (`RunnerConfiguration` / TOML)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `node_id` | `int` | *required* | Unique identifier for this node. Must match the node ids the mission layer uses in `node_ip_dict`. |
| `uav_api_port` | `int` | *required* | Port of the local UAV HTTP API (e.g. ArduPilot HTTP interface) running on `localhost`. Used for telemetry polling and sending mobility commands. |
| `control_api_port` | `int` | *required* | Dedicated plain-HTTP port for the control plane (`/mission`, `/protocols`, `/runs`). Separate from the data-plane port so the latter belongs entirely to the transport. Must be unique per node on a shared host. |
| `data_port` | `int` | *required* | Port this node binds for the inter-node data plane. Which transport serves it is chosen per mission at load. |
| `runs_dir` | `str` | `~/gradys_runs` | Directory holding one subdirectory per mission run. Served by `GET /runs`. |
| `protocols_dir` | `str` | `~/gradys_protocols` | Where uploaded protocols are written; added to `sys.path` so they import by module name. |
| `min_free_disk_mb` | `int` | `200` | Refuse to open a new run below this much free space. `0` disables the check. |
| `telemetry_landed_alt` | `float` | `0.5` | Relative altitude below which the vehicle counts as landed, which is how a `returning` mission closes out. |
| `certfile` | `str \| None` | `None` | TLS certificate (PEM), a file on this machine. For `"https"`/`"http3"`: the server cert, also the client trust anchor. For `"zenoh_quic"`: the QUIC TLS link identity **and** the fleet trust anchor — **must be the same on every node** (QUIC verifies peers against it). If `None`, an ephemeral self-signed cert is generated when the backend is built (fine for https/http3 with verification disabled, but `/mission/load` then rejects `zenoh_quic` outright). Ignored for `"http"`/`"zenoh_tcp"` missions. |
| `keyfile` | `str \| None` | `None` | TLS private key (PEM) paired with `certfile`. Required when `certfile` is provided. |

### Mission-level (`POST /mission/load` body)

| Parameter | Type | Default | Description |
|---|---|---|---|
| `protocol` | `str` | *required* | The protocol to fly, as `module:ClassName` (or a bare module holding exactly one protocol). |
| `node_ip_dict` | `dict[int, str]` | *required*¹ | Maps every node id to its bare `ip:port` data-plane address (no scheme). Identical on every node. Supplying it per mission is what lets a fleet gain or lose a drone without re-provisioning the rest. |
| `initial_position` | `list[float]` | `null` | Cartesian `(x, y, z)` target the drone flies to during `POST /mission/setup`; the z component is the takeoff altitude. Required before setup can fly. The one mission field that differs per drone. |
| `origin_gps_coordinates` | `list[float]` | `null` | Origin `(latitude, longitude, altitude)` of the shared cartesian frame. Identical fleet-wide. If omitted, the drone's own position at load becomes the origin — fine alone, never for a fleet. |
| `x_axis_degrees` | `float` | `null` | Clockwise rotation of the protocol's x-axis from true north (`0.0` keeps NEU: x=North, y=East). Identical fleet-wide. If omitted, taken from the drone's heading at load. |
| `communication_protocol` | `str` | `"http"` | Data-plane transport for this mission: `"http"`, `"https"`, `"http3"`, `"zenoh_tcp"` or `"zenoh_quic"`. Identical on every node. Does not affect the local UAV API connection, which is always plain HTTP on `localhost`. See [Message Transport](#message-transport-http-vs-https-vs-http3-vs-zenoh_tcp-vs-zenoh_quic). |
| `auto_scout` | `bool` | `false` | **Zenoh only.** `true` = Zenoh UDP multicast scouting discovers peers (and `node_ip_dict` becomes optional). No effect for the HTTP transports (accepted but ignored; a warning is logged). |
| `telemetry_interval` | `float` | `0.5` | Seconds between GPS telemetry polls during this mission. Lower values give more responsive position updates but increase load on the UAV API. |
| `label` | `str` | `null` | Optional suffix for the run directory name. |

¹ Required unless the transport is zenoh with `auto_scout=true`.

## Message Transport: http vs https vs http3 vs zenoh_tcp vs zenoh_quic

The inter-node data plane runs over one of five transports, selected per mission
by the `communication_protocol` field of `POST /mission/load`. The three HTTP transports serve an identical
FastAPI `/message` route and JSON payload — only the transport changes. The two `zenoh_*`
transports replace request/response POSTs with Eclipse Zenoh pub/sub in peer (p2p) mode, and
differ from each other only in link transport (TCP vs QUIC/TLS). `communication_protocol` must
be the same on every node; nodes using different transports cannot talk to each other.

| | `"http"` (default) | `"https"` | `"http3"` | `"zenoh_tcp"` | `"zenoh_quic"` |
|---|---|---|---|---|---|
| Model | request/response, IP-addressed | request/response, IP-addressed | request/response, IP-addressed | pub/sub, key-addressed, peer (p2p) | pub/sub, key-addressed, peer (p2p) |
| Server | uvicorn, HTTP/1.1 over TCP | uvicorn, HTTP/1.1 over TLS (TCP) | Hypercorn `quic_bind`, HTTP/3 over QUIC (UDP), TLS 1.3 | Zenoh peer session, **TCP** links | Zenoh peer session, **QUIC**/TLS 1.3 links |
| Addressing | `http://<addr>/message` | `https://<addr>/message` | `https://<addr>/message` | keys `gradys/msg/<dest_id>`, `gradys/msg/broadcast` | keys `gradys/msg/<dest_id>`, `gradys/msg/broadcast` |
| Discovery | `node_ip_dict` | `node_ip_dict` | `node_ip_dict` | `node_ip_dict` or multicast (`auto_scout`) | `node_ip_dict` or multicast (`auto_scout`) |
| TLS | none | server cert | server cert | none | **mandatory; fleet-wide shared cert** |
| Dependencies | core install | core install | `pip install "gradys-embedded[http3]"` | `pip install "gradys-embedded[zenoh]"` | `pip install "gradys-embedded[zenoh]"` |

The local UAV API connection is unaffected by `communication_protocol`; it always uses
plain HTTP on `localhost`.

### Zenoh (peer / p2p) transports — `zenoh_tcp` and `zenoh_quic`

With `communication_protocol="zenoh_tcp"` or `"zenoh_quic"`, each node opens one Zenoh session
in `peer` mode and subscribes to its inbox `gradys/msg/<node_id>` and to `gradys/msg/broadcast`.
SEND publishes to the destination's inbox key; BROADCAST is a **single** publish to the shared
broadcast key (not an O(n) per-peer loop). The wire payload is the same JSON `{"message",
"source"}` as the HTTP transports. The two transports differ only in the link layer:

- **`zenoh_tcp`** — `tcp/<ip:port>` links, no TLS.
- **`zenoh_quic`** — `quic/<ip:port>` links over TLS 1.3. QUIC **mandates** TLS and verifies each
  peer's certificate against a shared root CA (only hostname/SAN matching is disabled), so **every
  node must share the same `certfile`/`keyfile`**. Without a provisioned pair, `POST /mission/load`
  rejects `zenoh_quic` with HTTP 400 — an ephemeral per-node cert would make peers reject each
  other and the fleet silently fail to mesh. gradys-sitl-tester auto-generates one shared pair for
  a local fleet.

Peer discovery is controlled by `auto_scout` (same for both):

- `auto_scout=False` (default): multicast disabled; the session connects to explicit
  `tcp/<ip:port>` (or `quic/<ip:port>`) endpoints derived from `node_ip_dict`. Works on
  multicast-blocked LANs.
- `auto_scout=True`: Zenoh UDP multicast scouting (`224.0.0.224:7446`) auto-discovers peers;
  `node_ip_dict` is not needed for transport. Requires a multicast-capable network.

### How HTTP/3 and QUIC work

- **QUIC** is a transport protocol built on **UDP** instead of TCP. **HTTP/3** is HTTP
  running over QUIC.
- QUIC folds the transport handshake and the **TLS 1.3** encryption handshake into a
  single exchange (often 0–1 round trips), so connections set up faster than the
  separate TCP + TLS handshakes of HTTP/1.1.
- **Encryption is mandatory** — there is no unencrypted QUIC. That is why the server
  must present a certificate (see below).
- QUIC carries independent streams, so a single lost or delayed packet does not stall
  the others (no TCP head-of-line blocking). This helps on the lossy wireless/mesh
  links typical between drones.
- A QUIC connection is identified by a connection ID rather than the IP/port 4-tuple,
  so a drone that changes network path can keep the same connection
  (connection migration).

### TLS and certificates

The `"https"`, `"http3"`, and `"zenoh_quic"` transports all use TLS (QUIC mandates it; the
uvicorn HTTPS server is configured with a cert too):

- **`"https"`/`"http3"`** — with `certfile`/`keyfile` set, the server binds with them and clients
  **verify peers** against `certfile` (use the same pair fleet-wide for authenticated channels). If
  omitted, the server uses an **ephemeral self-signed certificate** and clients disable verification
  — encrypted but peers are **not authenticated**.
- **`"zenoh_quic"`** — the cert is the QUIC link identity **and** the fleet trust anchor. QUIC always
  verifies peers against it, so **every node must share the same `certfile`/`keyfile`** — which is
  why `/mission/load` rejects `zenoh_quic` outright (HTTP 400) when none is provisioned;
  gradys-sitl-tester auto-generates one shared pair for a local fleet.

For the full transport and security details, see
[`.claude/docs/cross-node-communication.md`](.claude/docs/cross-node-communication.md).

## Architecture

### Overview

GrADyS-Embedded is structured as a layered system that bridges protocol logic to real hardware:

```
+------------------+
|    IProtocol     |  Your protocol logic (portable across simulation and hardware)
+------------------+
        |
+------------------+
|  Encapsulator    |  Wraps protocol lifecycle, dispatches events
+------------------+
        |
+------------------+
|   IProvider      |  Translates abstract commands into real-world actions
| (EmbeddedProvider)
+------------------+
      /    \
     /      \
+--------+  +----------+
|  UAV   |  |  Other   |
|  API   |  |  Nodes   |
| (HTTP) |  |  (HTTP)  |
+--------+  +----------+
```

### Components

**Runner** (`EmbeddedRunner`) -- The entry point. `start_api()` creates the asyncio event loop and serves the control plane (`/mission`, `/protocols`, `/runs`) on `control_api_port`; the data plane (`/message`, or a Zenoh peer session) binds `data_port` when a mission loads. The runner owns everything process-scoped — loop, shared HTTP session, transport backend, telemetry poll — and delegates anything per-mission to the `MissionManager`.

**Mission manager** (`gradys_embedded/runner/mission.py`) -- Owns the mission state machine, the run directory, protocol resolution and the hot swap. Because both transports read `runner._encapsulator` per delivery, rebinding it swaps the protocol atomically without rebinding any port.

**Encapsulator** (`EmbeddedEncapsulator`) -- Wraps a protocol instance and connects it to the embedded provider. Delegates all lifecycle events (`initialize`, `handle_timer`, `handle_packet`, `handle_telemetry`, `finish`) to the protocol.

**Provider** (`EmbeddedProvider`) -- The `IProvider` implementation that makes the protocol's abstract commands concrete:

- **Communication commands** are delegated to the configured `CommunicationBackend`. `SEND` targets a specific node (its `/message` endpoint, or key `gradys/msg/<id>`); `BROADCAST` reaches every other node (one POST per peer for HTTP, or a single publish to `gradys/msg/broadcast` for zenoh).
- **Mobility commands** become HTTP calls to the local UAV API. `GOTO_COORDS` converts cartesian coordinates to GPS (using the configured origin) and calls `/movement/go_to_gps`. `GOTO_GEO_COORDS` calls the same endpoint directly. `SET_SPEED` calls `/command/set_air_speed`.
- **Timers** use the asyncio event loop's `call_at` for scheduling.

**Communication backends** (`gradys_embedded/communication/`) -- One `CommunicationBackend` per transport, built per mission by `create_backend(runner, context)`. Each owns both the data-plane *serve* (receive) side and the *send*/*broadcast* side: `http.py` covers `http`/`https`/`http3` (the `/message` FastAPI app + peer POSTs), `zenoh.py` covers `zenoh_tcp`/`zenoh_quic` (Zenoh peer session, no `/message` server). `certs.py` provides TLS material.

**Control API** (`gradys_embedded/runner/control_panel.py`) -- The `create_control_app` FastAPI application: `/protocols`, `/mission` and `/runs`. Served on `control_api_port`, driving the mission lifecycle from outside the process — the only gateway for mission parameters. Always plain HTTP, independent of the mission's transport. The `/message` data-plane app lives with the HTTP backend in `communication/http.py`.

**Position Utilities** -- Functions for converting between GPS coordinates and a local cartesian frame (North-East-Up) using haversine distance calculations. All nodes must share the same `origin_gps_coordinates` so their cartesian frames are consistent.

### Data Flows

**Telemetry** -- The runner periodically polls `GET /telemetry/gps` from the UAV API, converts the GPS response to cartesian coordinates relative to the configured origin, and delivers a `Telemetry` object to the protocol via `handle_telemetry`.

**Mobility** -- When a protocol sends a mobility command through the provider, coordinates are converted from cartesian to GPS (if needed) and forwarded to the UAV API via HTTP.

**Communication** -- When a protocol sends a message, the provider delegates to the configured `CommunicationBackend`, which performs an HTTP POST to the destination node's message API (http/https/http3) or a Zenoh publish on the shared peer session (`zenoh_tcp`/`zenoh_quic`). On the receiving side, the FastAPI `/message` endpoint — or, for Zenoh, the subscriber callback marshalled onto the event loop — delivers the payload to the protocol's `handle_packet` method.

## License

MIT

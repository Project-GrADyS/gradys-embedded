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

**Dependencies:** `fastapi`, `uvicorn`, `aiohttp`, `pydantic`.

HTTP/3 (QUIC) message transport (`udp=True`) is an optional extra:

```bash
pip install "gradys-embedded[udp]"   # adds hypercorn[h3], niquests, cryptography
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

### 2. Configure and run

```python
from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

configuration = RunnerConfiguration(
    node_id=1,
    node_ip_dict={
        1: "192.168.1.10:5000",
        2: "192.168.1.11:5000",
        3: "192.168.1.12:5000",
    },
    uav_api_port=8000,
    origin_gps_coordinates=(-15.840081, -47.926642, 0.0),
)

runner = EmbeddedRunner(configuration, MyProtocol)
runner.start_api()
```

`start_api()` is the only public entry point. It owns the asyncio loop and serves a FastAPI app with two routers:

- `POST /message` — inter-node delivery (forwarded to the protocol's `handle_packet`).
- `POST /protocol/setup` — arms the drone, takes off, and flies to `initial_position`.
- `POST /protocol/start` — instantiates the protocol, calls `initialize()`, and starts the telemetry polling loop.

After launching the runner, an external client (operator console, sitl-tester, `curl`) drives the drone end-to-end:

```bash
curl -X POST http://<drone>:<port>/protocol/setup
curl -X POST http://<drone>:<port>/protocol/start
```

Each node in the network runs its own instance of `EmbeddedRunner` with a unique `node_id`.

## Configuration Parameters

The `RunnerConfiguration` dataclass controls how the runner connects to the UAV and to other nodes.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `node_id` | `int` | *required* | Unique identifier for this node in the network. Must match a key in `node_ip_dict`. |
| `node_ip_dict` | `dict[int, str]` | *required* | Maps every node ID in the network to its `ip:port` address for inter-node messaging. Each node uses this to know where to send messages. |
| `uav_api_port` | `int` | *required* | Port of the local UAV HTTP API (e.g. ArduPilot HTTP interface) running on `localhost`. Used for telemetry polling and sending mobility commands. |
| `initial_position` | `tuple[float, float, float]` | *required* | Cartesian `(x, y, z)` target the drone flies to during `POST /protocol/setup` (after arm + takeoff). |
| `origin_gps_coordinates` | `tuple[float, float, float] \| None` | `None` | Reference GPS point `(latitude, longitude, altitude)` used as the origin for converting between GPS and cartesian coordinates. All nodes in the network must share the same origin. If `None`, the drone's current position at boot is used. |
| `x_axis_degrees` | `float \| None` | `None` | Clockwise rotation of the protocol's x-axis from true north, in degrees (`0.0` keeps the NEU convention: x=North, y=East). Must be identical on every node. If `None`, it is taken from the drone's heading at boot. |
| `telemetry_interval` | `float` | `0.5` | Seconds between GPS telemetry polls from the UAV API. Lower values give more responsive position updates but increase load on the UAV API. |
| `udp` | `bool` | `False` | Serve the inter-node message API over QUIC/HTTP3 (UDP) via Hypercorn instead of HTTP/TCP via uvicorn. Must be identical on every node. Does not affect the local UAV API connection, which is always plain HTTP on `localhost`. See [Message Transport](#message-transport-tcp-vs-http3-quic). |
| `certfile` | `str \| None` | `None` | TLS certificate (PEM) for the QUIC server in `udp` mode; also used as the client trust anchor to verify peers. If `None`, an ephemeral self-signed certificate is generated at boot and peer verification is disabled. Ignored when `udp` is `False`. |
| `keyfile` | `str \| None` | `None` | TLS private key (PEM) paired with `certfile`. Required when `certfile` is provided. Ignored when `udp` is `False` or `certfile` is `None`. |

## Message Transport: TCP vs HTTP/3 (QUIC)

The inter-node message API runs over one of two transports, selected by the `udp`
configuration flag. By default (`udp=False`) it is served as HTTP/1.1 over TCP via
uvicorn. Set `udp=True` to serve it as HTTP/3 over QUIC via Hypercorn instead. The
FastAPI app, the `/message` route, and the JSON payload are **identical** in both
modes — only the transport changes. `udp` must be the same on every node; nodes using
different transports cannot talk to each other.

| | `udp=False` (default) | `udp=True` |
|---|---|---|
| Server | uvicorn, HTTP/1.1 over TCP | Hypercorn `quic_bind`, HTTP/3 over QUIC (UDP), TLS 1.3 |
| Peer URL scheme | `http://<addr>/message` | `https://<addr>/message` |
| Client | shared `aiohttp.ClientSession` | `niquests.AsyncSession` |
| Dependencies | core install | `pip install "gradys-embedded[udp]"` |

The local UAV API connection is unaffected by `udp`; it always uses plain HTTP on
`localhost`.

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

Because QUIC requires TLS, the `udp=True` server always presents a certificate:

- With `certfile`/`keyfile` set, the server binds with them and clients **verify peers**
  against `certfile`. Use the same pair across the fleet for authenticated channels.
- If they are omitted, the server uses an **ephemeral self-signed certificate** generated
  at boot and clients set `verify=False` — the channel is encrypted but peers are
  **not authenticated**.

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

**Runner** (`EmbeddedRunner`) -- The entry point. Exposes a single public method, `start_api()`, which creates the asyncio event loop and schedules a task that serves a FastAPI app with `/message`, `/protocol/setup`, and `/protocol/start` endpoints. `/protocol/setup` arms and takes off; `/protocol/start` bootstraps the encapsulator, calls the protocol's `initialize`, and begins the telemetry polling loop.

**Encapsulator** (`EmbeddedEncapsulator`) -- Wraps a protocol instance and connects it to the embedded provider. Delegates all lifecycle events (`initialize`, `handle_timer`, `handle_packet`, `handle_telemetry`, `finish`) to the protocol.

**Provider** (`EmbeddedProvider`) -- The `IProvider` implementation that makes the protocol's abstract commands concrete:

- **Communication commands** become HTTP POST requests. `SEND` posts to a specific node's `/message` endpoint; `BROADCAST` posts to every other node.
- **Mobility commands** become HTTP calls to the local UAV API. `GOTO_COORDS` converts cartesian coordinates to GPS (using the configured origin) and calls `/movement/go_to_gps`. `GOTO_GEO_COORDS` calls the same endpoint directly. `SET_SPEED` calls `/command/set_air_speed`.
- **Timers** use the asyncio event loop's `call_at` for scheduling.

**Message API** -- A FastAPI application that bundles two routers on the port specified in `node_ip_dict` for the node's own id: a message router (`POST /message`, forwarded to `handle_packet`) and a protocol router (`POST /protocol/setup`, `POST /protocol/start`) that drives the runner's lifecycle from outside the process.

**Position Utilities** -- Functions for converting between GPS coordinates and a local cartesian frame (North-East-Up) using haversine distance calculations. All nodes must share the same `origin_gps_coordinates` so their cartesian frames are consistent.

### Data Flows

**Telemetry** -- The runner periodically polls `GET /telemetry/gps` from the UAV API, converts the GPS response to cartesian coordinates relative to the configured origin, and delivers a `Telemetry` object to the protocol via `handle_telemetry`.

**Mobility** -- When a protocol sends a mobility command through the provider, coordinates are converted from cartesian to GPS (if needed) and forwarded to the UAV API via HTTP.

**Communication** -- When a protocol sends a message, the provider performs an HTTP POST to the destination node's message API. On the receiving side, the FastAPI endpoint delivers the message payload to the protocol's `handle_packet` method.

## License

MIT

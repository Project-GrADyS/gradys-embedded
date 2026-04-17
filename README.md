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
runner.run()
```

Each node in the network runs its own instance of `EmbeddedRunner` with a unique `node_id`.

## Configuration Parameters

The `RunnerConfiguration` dataclass controls how the runner connects to the UAV and to other nodes.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `node_id` | `int` | *required* | Unique identifier for this node in the network. Must match a key in `node_ip_dict`. |
| `node_ip_dict` | `dict[int, str]` | *required* | Maps every node ID in the network to its `ip:port` address for inter-node messaging. Each node uses this to know where to send messages. |
| `uav_api_port` | `int` | *required* | Port of the local UAV HTTP API (e.g. ArduPilot HTTP interface) running on `localhost`. Used for telemetry polling and sending mobility commands. |
| `origin_gps_coordinates` | `tuple[float, float, float]` | *required* | Reference GPS point `(latitude, longitude, altitude)` used as the origin for converting between GPS and cartesian coordinates. All nodes in the network must share the same origin. |
| `telemetry_interval` | `float` | `0.5` | Seconds between GPS telemetry polls from the UAV API. Lower values give more responsive position updates but increase load on the UAV API. |

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

**Runner** (`EmbeddedRunner`) -- The entry point. Creates an asyncio event loop, bootstraps the encapsulator, starts a FastAPI message server for receiving inter-node messages, and runs a periodic telemetry polling loop against the local UAV API.

**Encapsulator** (`EmbeddedEncapsulator`) -- Wraps a protocol instance and connects it to the embedded provider. Delegates all lifecycle events (`initialize`, `handle_timer`, `handle_packet`, `handle_telemetry`, `finish`) to the protocol.

**Provider** (`EmbeddedProvider`) -- The `IProvider` implementation that makes the protocol's abstract commands concrete:

- **Communication commands** become HTTP POST requests. `SEND` posts to a specific node's `/message` endpoint; `BROADCAST` posts to every other node.
- **Mobility commands** become HTTP calls to the local UAV API. `GOTO_COORDS` converts cartesian coordinates to GPS (using the configured origin) and calls `/movement/go_to_gps`. `GOTO_GEO_COORDS` calls the same endpoint directly. `SET_SPEED` calls `/command/set_air_speed`.
- **Timers** use the asyncio event loop's `call_at` for scheduling.

**Message API** -- A FastAPI application with a single `POST /message` endpoint. Each node runs one on the port specified in `node_ip_dict` for its own `node_id`. When a message arrives, it is forwarded to the protocol via `handle_packet`.

**Position Utilities** -- Functions for converting between GPS coordinates and a local cartesian frame (North-East-Up) using haversine distance calculations. All nodes must share the same `origin_gps_coordinates` so their cartesian frames are consistent.

### Data Flows

**Telemetry** -- The runner periodically polls `GET /telemetry/gps` from the UAV API, converts the GPS response to cartesian coordinates relative to the configured origin, and delivers a `Telemetry` object to the protocol via `handle_telemetry`.

**Mobility** -- When a protocol sends a mobility command through the provider, coordinates are converted from cartesian to GPS (if needed) and forwarded to the UAV API via HTTP.

**Communication** -- When a protocol sends a message, the provider performs an HTTP POST to the destination node's message API. On the receiving side, the FastAPI endpoint delivers the message payload to the protocol's `handle_packet` method.

## License

MIT

# GrADyS-Embedded

Runs [GrADyS-Sim NextGen](https://project-gradys.github.io/gradys-sim-nextgen/) `IProtocol` implementations on real quadcopters. The exact same protocol class that runs in simulation runs here — `EmbeddedEncapsulator` + `EmbeddedProvider` translate `IProvider` calls into HTTP against a local `uav_api` and FastAPI endpoints on peer nodes.

The **interface contract is owned by `gradys-sim-nextgen`**. This project implements it on top of an asyncio loop and HTTP; it does not redefine protocol semantics.

## Quick start

```bash
pip install -e .       # or: pip install gradys-embedded
# requires: fastapi, uvicorn, aiohttp, pydantic
```

A node boots like this (see `examples/simple/ge.py`):

```python
from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration
from protocol import SimpleUAVProtocol

config = RunnerConfiguration(
    node_id=1,
    node_ip_dict={
        1: "192.168.1.10:5000",
        2: "192.168.1.11:5000",
    },
    uav_api_port=8000,
    origin_gps_coordinates=(-15.840081, -47.926642, -0.016),
    initial_position=(0, 0, 20),
)

runner = EmbeddedRunner(config, SimpleUAVProtocol)
runner.start_api()    # owns the loop; serves /message + /protocol routers
```

`start_api()` is the only public method. The runner does **not** arm, take off, or instantiate the protocol on its own — those are driven by HTTP calls into the `/protocol` router after the API is up:

```bash
curl -X POST http://<drone>:<port>/protocol/setup   # arm + takeoff + go to initial_position
curl -X POST http://<drone>:<port>/protocol/start   # initialize protocol + begin telemetry polling
```

Each drone runs its own `EmbeddedRunner` with a unique `node_id`; `uav_api` must be running locally on `uav_api_port` on that drone before `/protocol/setup` is called.

## Hardware gotchas

These are the things that bite you on real flights and don't show up in simulation.

1. **Every node must share the same `origin_gps_coordinates`.** Positions in protocol code are cartesian (NEU: x=North, y=East, z=Up, meters). If two nodes use different origins, their cartesian frames disagree and messages like "go to (50, 0, 20)" mean different places. No code check enforces this — it is an operational invariant.
2. **`uav_api` must be reachable at `http://localhost:<uav_api_port>` before `POST /protocol/setup` is called.** That handler runs `arm → takeoff → go_to_gps_wait(initial_position)`; any failure returns HTTP 500 and `/protocol/start` will refuse with 409 until setup succeeds.
3. **Outbound communication is fire-and-forget.** `SendMessageCommand` / `BroadcastMessageCommand` schedule an `aiohttp.post` as an asyncio task; failures are logged but the protocol is never told a peer was unreachable. Design protocols assuming unreliable delivery.
4. **`handle_telemetry` is polled, not pushed.** Default poll interval is `telemetry_interval=0.5` s. A protocol that relies on sub-second position reaction (tight waypoint tolerance, fast-moving objects) needs a lower interval and a UAV API that can keep up.
5. **Asyncio-only.** The provider schedules timers with `loop.call_at`, and all HTTP traffic runs on a single `asyncio.new_event_loop()`. Blocking calls inside protocol hooks freeze the message server and telemetry loop. Keep `handle_*` methods non-blocking.
6. **`current_time()` is `loop.time()` — a monotonic clock.** It is *not* GPS time, UTC, or wall-clock time. Cross-node time comparisons require a separate synchronization mechanism.
7. **`node_ip_dict` is the peer directory and cannot be changed at runtime.** Every node must already know every other node's `ip:port`. There is no discovery protocol.

## Key concepts

- **`EmbeddedRunner`** (`gradys_embedded/runner/runner.py`) — entry point; sole public method `start_api()` owns the asyncio loop and serves the unified FastAPI app. Setup (arm/takeoff) and start (encapsulator + telemetry) are triggered by `POST /protocol/setup` and `POST /protocol/start`.
- **`EmbeddedEncapsulator`** (`gradys_embedded/encapsulator/embedded.py`) — wraps a protocol and delegates the five `IProtocol` hooks.
- **`EmbeddedProvider`** (same file) — the `IProvider` implementation that turns abstract commands into HTTP against `uav_api` and peer nodes, plus `loop.call_at` timers.
- **`RunnerConfiguration`** (`gradys_embedded/runner/configuration.py`) — `node_id`, `node_ip_dict`, `origin_gps_coordinates`, `initial_position`, `uav_api_port`, `telemetry_interval`.
- **Message API** (`gradys_embedded/runner/message_api.py`) — a FastAPI app with `POST /message` that each node runs on its own port.

## Directories

| Path | Purpose |
|---|---|
| `gradys_embedded/runner/` | `EmbeddedRunner`, `RunnerConfiguration`, FastAPI app (`message_api.py`: `message` + `protocol` routers) |
| `gradys_embedded/encapsulator/` | `IEncapsulator`, `EmbeddedEncapsulator`, `EmbeddedProvider` |
| `gradys_embedded/protocol/` | Mirrors sim-nextgen: `interface.py` (IProtocol/IProvider), `messages/`, `position.py`, `plugin/` |
| `examples/simple/` | Sensor/UAV/ground-station trio; reference for wiring `ge.py` + `protocol.py` |

## When to open which doc

- `→ .claude/docs/runtime-model.md` — `EmbeddedRunner` lifecycle (`start_api`, `/protocol/setup`, `/protocol/start`, telemetry polling, shutdown). Open when debugging startup, boot order, or the asyncio loop.
- `→ .claude/docs/protocol-interface.md` — embedded-specific **implementation notes** only. For the authoritative IProtocol/IProvider contract, follow the pointer inside this doc to `gradys-sim-nextgen`.
- `→ .claude/docs/mobility-and-telemetry.md` — NEU↔GPS conversion, `MobilityCommand` → uav_api endpoint map, telemetry polling path. Open when movement or positioning is involved.
- `→ .claude/docs/cross-node-communication.md` — `/message` FastAPI endpoint, SEND vs BROADCAST HTTP semantics, fire-and-forget failure modes. Open when a message is not arriving.
- `→ .claude/docs/configuration.md` — every `RunnerConfiguration` field, the shared-origin invariant, network topology rules. Open when setting up a new deployment.
- `→ .claude/docs/encapsulator-interface.md` — `IEncapsulator` / `EmbeddedEncapsulator` / `EmbeddedProvider` delegation chain, async dispatch, `loop.call_at` timers. Open when wiring a new provider capability or debugging a missing callback.
- `→ .claude/docs/plugins-and-extensions.md` — dispatcher pattern, mission/random/follow-mobility plugins, Raft, Statistics. Open when composing protocol behavior.

## Cross-project pointers

- `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/CLAUDE.md` — the simulator. The same `IProtocol` subclass runs there; the authoritative interface spec is `.claude/docs/protocol-lifecycle.md` in that project.
- `→ /home/fleury/gradys/major_projects/uav_api/.claude/docs/specification.md` — **authoritative** HTTP contract for every endpoint this project hits on `localhost:uav_api_port`. Update it first if you add or change a command.

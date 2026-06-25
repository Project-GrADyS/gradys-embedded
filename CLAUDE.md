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
    control_api_port=6000,
    origin_gps_coordinates=(-15.840081, -47.926642, -0.016),
    initial_position=(0, 0, 20),
)

runner = EmbeddedRunner(config, SimpleUAVProtocol)
runner.start_api()    # owns the loop; serves control plane + data plane as two servers
```

`start_api()` is the only public method. The runner does **not** arm, take off, or instantiate the protocol on its own — those are driven by HTTP calls into the `/protocol` **control plane**, which runs on `control_api_port` (separate from the data-plane message port):

```bash
curl -X POST http://<drone>:<control_api_port>/protocol/setup   # arm + takeoff + go to initial_position
curl -X POST http://<drone>:<control_api_port>/protocol/start   # initialize protocol + begin telemetry polling
```

Each drone runs its own `EmbeddedRunner` with a unique `node_id`; `uav_api` must be running locally on `uav_api_port` on that drone before `/protocol/setup` is called. The control plane (`/protocol/*`) is served on `control_api_port`; the inter-node data plane (`/message`, or Zenoh) is served separately on the drone's `node_ip_dict` port.

## Hardware gotchas

These are the things that bite you on real flights and don't show up in simulation.

1. **Every node must share the same `origin_gps_coordinates`.** Positions in protocol code are cartesian (NEU: x=North, y=East, z=Up, meters). If two nodes use different origins, their cartesian frames disagree and messages like "go to (50, 0, 20)" mean different places. No code check enforces this — it is an operational invariant.
2. **`uav_api` must be reachable at `http://localhost:<uav_api_port>` before `POST /protocol/setup` is called.** That handler runs `arm → takeoff → go_to_gps_wait(initial_position)`; any failure returns HTTP 500 and `/protocol/start` will refuse with 409 until setup succeeds. Note `/protocol/setup` and `/protocol/start` are served on `control_api_port`, **not** the data-plane message port.
3. **Outbound communication is fire-and-forget.** `SendMessageCommand` / `BroadcastMessageCommand` schedule an `aiohttp.post` as an asyncio task; failures are logged but the protocol is never told a peer was unreachable. Design protocols assuming unreliable delivery.
4. **`handle_telemetry` is polled, not pushed.** Default poll interval is `telemetry_interval=0.5` s. A protocol that relies on sub-second position reaction (tight waypoint tolerance, fast-moving objects) needs a lower interval and a UAV API that can keep up.
5. **Asyncio-only.** The provider schedules timers with `loop.call_at`, and all HTTP traffic runs on a single `asyncio.new_event_loop()`. Blocking calls inside protocol hooks freeze the message server and telemetry loop. Keep `handle_*` methods non-blocking.
6. **`current_time()` is `loop.time()` — a monotonic clock.** It is *not* GPS time, UTC, or wall-clock time. Cross-node time comparisons require a separate synchronization mechanism.
7. **`node_ip_dict` is the peer directory and cannot be changed at runtime.** Every node must already know every other node's `ip:port`. There is no discovery protocol — **except** `communication_protocol="zenoh_tcp"`/`"zenoh_quic"` with `auto_scout=True`, which uses Zenoh multicast scouting to discover peers automatically (requires a multicast-capable LAN).
8. **`communication_protocol` must match across the fleet.** It selects the inter-node data-plane transport — `"http"` (default, HTTP/1.1 over TCP via uvicorn), `"https"` (HTTP/1.1 over TLS via uvicorn), `"http3"` (QUIC/HTTP3 via Hypercorn, requires `pip install "gradys-embedded[http3]"`), `"zenoh_tcp"` (Eclipse Zenoh pub/sub in peer/p2p mode over TCP, requires `pip install "gradys-embedded[zenoh]"`), or `"zenoh_quic"` (same Zenoh pub/sub over QUIC/TLS 1.3). **`zenoh_quic` mandates TLS and needs a fleet-wide shared `certfile`/`keyfile` (identical on every node)** — with the default ephemeral per-node cert, peers reject each other; gradys-sitl-tester auto-generates a shared pair. Mixed transports cannot talk to each other. Unrelated to `uav_api`'s own `--udp` flag — the local `uav_api` connection is always plain HTTP on `localhost`.
9. **`auto_scout` only does something for the zenoh transports.** `True` = Zenoh multicast scouting (auto-discovery); `False` (default) = explicit peer endpoints from `node_ip_dict`. It is accepted but **ignored** for http/https/http3 (a warning is logged).

## Key concepts

- **`EmbeddedRunner`** (`gradys_embedded/runner/runner.py`) — entry point; sole public method `start_api()` owns the asyncio loop and serves **two** servers: the control plane (`/protocol/*`) on `control_api_port` and the data plane (`/message`, or Zenoh) on the `node_ip_dict` port. Setup (arm/takeoff) and start (encapsulator + telemetry) are triggered by `POST /protocol/setup` and `POST /protocol/start` on the control port.
- **`EmbeddedEncapsulator`** (`gradys_embedded/encapsulator/embedded.py`) — wraps a protocol and delegates the five `IProtocol` hooks.
- **`EmbeddedProvider`** (same file) — the `IProvider` implementation that turns abstract commands into HTTP against `uav_api` and peer nodes, plus `loop.call_at` timers.
- **`RunnerConfiguration`** (`gradys_embedded/runner/configuration.py`) — `node_id`, `node_ip_dict`, `initial_position`, `uav_api_port`, `control_api_port`, `origin_gps_coordinates`, `telemetry_interval`, `communication_protocol`, `auto_scout` (+ optional `certfile`/`keyfile`).
- **Communication backends** (`gradys_embedded/communication/`) — one `CommunicationBackend` per transport, owning both the data-plane *serve* (receive) and *send*/*broadcast* sides. `http.py` = `http`/`https`/`http3` (shared `/message` FastAPI app + peer-`POST`); `zenoh.py` = `zenoh_tcp`/`zenoh_quic` (Zenoh peer pub/sub); `certs.py` = TLS material; `__init__.create_backend(runner)` is the factory. The runner runs `backend.serve()`; the provider calls `backend.send`/`broadcast`.
- **Control API** (`gradys_embedded/runner/control_panel.py`) — `create_control_app` only: the `/protocol/*` control plane, always plain HTTP on `control_api_port`. (The `/message` data-plane app moved into `communication/http.py`.) When `communication_protocol` is a zenoh transport there is no `/message` server — a Zenoh peer session carries messages instead.

## Directories

| Path | Purpose |
|---|---|
| `gradys_embedded/runner/` | `EmbeddedRunner`, `RunnerConfiguration`, control-plane app (`control_panel.py`: `/protocol/*` router) |
| `gradys_embedded/communication/` | Per-transport `CommunicationBackend`s (`http.py`, `zenoh.py`), `certs.py`, `create_backend` factory |
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

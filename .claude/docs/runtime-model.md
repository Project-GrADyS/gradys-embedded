# Runtime Model

How `EmbeddedRunner` boots, runs, and shuts down. Source: `gradys_embedded/runner/runner.py`, `gradys_embedded/runner/control_panel.py`.

## The single public entry point

```python
runner = EmbeddedRunner(config, MyProtocol)
runner.start_api()    # blocks; owns the asyncio loop
```

`start_api()` is the only public method. It does **not** arm the drone or instantiate the protocol — those happen later, when an external client POSTs to the protocol router.

After `start_api()` returns control to uvicorn, lifecycle is driven over HTTP. The control plane
and the data plane are **two independent servers** (see below):

```
control plane  (control_api_port, always plain HTTP):
  POST /protocol/setup   → arm + takeoff + go to initial_position
  POST /protocol/start   → instantiate protocol, initialize, begin telemetry polling

data plane     (node_ip_dict port, transport = communication_protocol):
  POST /message          → inter-node delivery for http/https/http3 (409 until /protocol/start)
  (zenoh)                → inter-node delivery via Zenoh pub/sub instead of POST /message
```

## `start_api()` — what it does

1. `logging.basicConfig(level=INFO, ...)`.
2. `self._loop = asyncio.new_event_loop()` and `asyncio.set_event_loop(self._loop)` — the runner owns the loop.
3. Schedules `self._serve_api()` as a task on that loop.
4. `self._loop.run_forever()` — blocks until `KeyboardInterrupt`.
5. `finally`: `encapsulator.finish()` if the protocol was started; close the aiohttp session; close the loop.

`_serve_communication()` is the task that boots HTTP:

1. Creates `self._session = aiohttp.ClientSession()` on the runner-owned loop. Every subsequent HTTP call (setup, telemetry, peer sends) reuses this session.
2. Builds the control app (`create_control_app(self)`, the `/protocol/*` router) and the data-plane backend (`create_backend(self)` from `gradys_embedded.communication`, selected by `communication_protocol`).
3. Runs **two servers concurrently** via `asyncio.gather`:
   - **Control plane** — `_serve_control` runs the control app on `control_api_port` over plain HTTP, for every transport.
   - **Data plane** — `self._backend.serve()`. The `HttpBackend` (http/https/http3) serves its `/message` app on the `node_ip_dict` port via uvicorn/Hypercorn; the `ZenohBackend` (zenoh_tcp/zenoh_quic) opens a Zenoh peer session instead (no uvicorn message server). Both run for the process lifetime. The backend resolves its own bind port from `node_ip_dict[node_id]`.

To avoid two uvicorn servers racing to install process-wide signal handlers, both the control server and the HTTP backend disable per-server signal handling; the runner's own `KeyboardInterrupt` path owns shutdown (and calls `self._backend.close()`).

Because `_serve_communication` creates the session **before** starting the servers, by the time they bind the session is already attached to the runner. Endpoint handlers can rely on `runner._session` being non-None.

## The control app and the data-plane backend

The `/protocol/*` and `/message` routers used to share one app on one port. The control plane is now
its own app on `control_api_port` (`control_panel.create_control_app(runner)`), and the data plane is
owned by a `CommunicationBackend` (`gradys_embedded/communication/`) — for the HTTP transports the
backend serves the `/message` app (`communication.http.build_message_app(runner)`) on the
`node_ip_dict` port; for the zenoh transports there is no `/message` app at all. The control router:

### Message app — `POST /message` (HTTP transports only)

Built by `communication.http.build_message_app(runner)` and served by the `HttpBackend`:

```python
@router.post("/message")
async def receive_message(payload: MessagePayload):
    if runner._encapsulator is None:
        raise HTTPException(409, "Protocol not started")
    runner._encapsulator.handle_packet(payload.message)
    return {"status": "ok"}
```

The 409 guard prevents `AttributeError` if a peer sends a message before this node has finished `/protocol/start`. The zenoh backends have no `/message` app — their subscriber callback applies the same drop-before-start guard. Wire format and full message semantics: `→ .claude/docs/cross-node-communication.md`.

### Protocol router — `POST /protocol/setup`, `POST /protocol/start`

- `POST /protocol/setup` awaits `runner._goto_initial_position()`. On success, sets `runner._setup_done = True` and returns 200. On any arm/takeoff/goto failure, returns 500 (and `_setup_done` stays False so the operator can retry). Returns 409 if already set up.
- `POST /protocol/start` awaits `runner._bootstrap_protocol()`. Returns 409 if `/protocol/setup` has not succeeded, or if start has already run. On success returns 200.

Both endpoints run **on the runner-owned loop** (uvicorn is just a task on that loop), so `await`-ing the runner's helpers does not require any thread-bridging.

## `_goto_initial_position` — what `/protocol/setup` runs

Three HTTP calls against `http://localhost:<uav_api_port>`, all using the shared `aiohttp.ClientSession`:

1. `GET /command/arm` — arms the flight controller. Any non-200 logs fatal and returns False.
2. `GET /command/takeoff?alt=<initial_position[2]>` — takes off to the configured altitude.
3. `POST /movement/go_to_gps_wait` — converts `initial_position` (cartesian NEU, meters) to GPS via `cartesian_to_geo(origin_gps_coordinates, initial_position)` and **waits for the drone to arrive** before returning.

Returns True if all three succeed.

## `_bootstrap_protocol` — what `/protocol/start` runs

```python
async def _bootstrap_protocol(self):
    self._encapsulator = EmbeddedEncapsulator(config, self._loop, self._session, backend=self._backend)
    self._encapsulator.encapsulate(self._protocol_class)  # instantiate protocol
    self._encapsulator.initialize()                        # call protocol.initialize()

    self._loop.create_task(self._periodic_telemetry())
```

The encapsulator receives `self._backend` (the `CommunicationBackend` built in `_serve_communication`), and the provider delegates SEND/BROADCAST to it. For the zenoh transports, `backend.serve()` (which runs from `start_api`, before `/protocol/start`) has already opened the shared peer session that `backend.send`/`broadcast` publish on; its subscriber callback, like the `/message` 409 guard, drops inbound samples while `self._encapsulator is None` (before start).

The protocol's `initialize` runs **before** the telemetry loop is started. The data plane has been bound since `start_api` started the backend — the `/message` endpoint (HTTP transports) just rejected with 409, and the zenoh subscriber just dropped samples, until `_bootstrap_protocol` populated `self._encapsulator`.

Practical consequence: messages broadcast from `initialize` will reach peers whose own `/protocol/start` has already run, but will be rejected by peers still in the setup phase. Boot ordering across drones is uncoordinated — design protocols to tolerate dropped bootstrap broadcasts, or delay the first broadcast with a short timer.

## Telemetry loop — `_periodic_telemetry`

Infinite coroutine started by `_bootstrap_protocol`:

```python
while True:
    try:
        async with session.get(f"{base_url}/telemetry/gps") as resp:
            data = await resp.json()
        pos = data["info"]["position"]
        geo = (pos["lat"], pos["lon"], pos["relative_alt"])
        cartesian = geo_to_cartesian(origin_gps_coordinates, geo)
        encapsulator.handle_telemetry(Telemetry(current_position=cartesian))
    except Exception as e:
        self._logger.error(f"Telemetry fetch failed: {e}")
    await asyncio.sleep(interval)
```

Key points:

- **The altitude used is `relative_alt`** (meters above takeoff), not absolute altitude. Protocols see z relative to the drone's home point, which aligns with the cartesian NEU frame.
- **A failed telemetry fetch is swallowed.** The loop retries at the next interval; the protocol receives no explicit signal that telemetry dropped. Detect stalls by timestamping telemetry inside the protocol if you care.
- **`handle_telemetry` is called from the asyncio loop.** If the protocol blocks inside this hook, the next telemetry and every other loop task pile up.

Conversion details and the NEU convention: `→ .claude/docs/mobility-and-telemetry.md`.

## Shutdown

The runner's `finally` block in `start_api()`:

1. Calls `encapsulator.finish()` if a protocol was started — the protocol's `finish()` runs. The loop is about to close, so `finish()` cannot rely on async operations or peer HTTP calls resolving.
2. Closes the aiohttp session (`run_until_complete(session.close())`).
3. Closes the event loop.

**Do not issue outbound communication commands from `finish()`** — they schedule aiohttp tasks on a loop that is closing.

`KeyboardInterrupt` (Ctrl-C) is the intended shutdown path. SIGTERM reaches the same `finally` because `run_forever()` raises on signal.

## Threading / concurrency model summary

- **One process per drone.**
- **One asyncio loop** owned by `EmbeddedRunner`. Two servers (control + data plane) run cooperatively on it.
- **No threads for HTTP.** `uvicorn.Server` runs cooperatively on the same loop (`loop="asyncio"`).
- **Exception: Zenoh.** Its subscriber callback fires on a Zenoh-owned background thread; the callback only marshals onto the loop via `self._loop.call_soon_threadsafe(self._encapsulator.handle_packet, message)` — it must never touch protocol/loop state directly.
- **Blocking inside any `handle_*` hook freezes everything** — including the `/protocol` and `/message` endpoints.

## Related docs

- `→ .claude/docs/configuration.md` — what `RunnerConfiguration` fields control and their invariants.
- `→ .claude/docs/encapsulator-interface.md` — how `EmbeddedEncapsulator` and `EmbeddedProvider` translate calls once the loop is running.
- `→ .claude/docs/protocol-interface.md` — how the five `IProtocol` hooks are invoked from this loop.
- `→ .claude/docs/mobility-and-telemetry.md` — coordinate frames and the telemetry fetch path.
- `→ .claude/docs/cross-node-communication.md` — the `/message` router and peer HTTP.

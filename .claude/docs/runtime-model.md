# Runtime Model

How `EmbeddedRunner` boots, runs, and shuts down. Source: `gradys_embedded/runner/runner.py`, `gradys_embedded/runner/message_api.py`.

## The single public entry point

```python
runner = EmbeddedRunner(config, MyProtocol)
runner.start_api()    # blocks; owns the asyncio loop
```

`start_api()` is the only public method. It does **not** arm the drone or instantiate the protocol — those happen later, when an external client POSTs to the protocol router.

After `start_api()` returns control to uvicorn, lifecycle is driven over HTTP:

```
POST /protocol/setup   → arm + takeoff + go to initial_position
POST /protocol/start   → instantiate protocol, initialize, begin telemetry polling
POST /message          → inter-node delivery (always available; 409 if /protocol/start has not run yet)
```

## `start_api()` — what it does

1. `logging.basicConfig(level=INFO, ...)`.
2. `self._loop = asyncio.new_event_loop()` and `asyncio.set_event_loop(self._loop)` — the runner owns the loop.
3. Schedules `self._serve_api()` as a task on that loop.
4. `self._loop.run_forever()` — blocks until `KeyboardInterrupt`.
5. `finally`: `encapsulator.finish()` if the protocol was started; close the aiohttp session; close the loop.

`_serve_api()` is the task that boots HTTP:

1. Creates `self._session = aiohttp.ClientSession()` on the runner-owned loop. Every subsequent HTTP call (setup, telemetry, peer sends) reuses this session.
2. Resolves the bind port from `node_ip_dict[node_id]` (`"host:port"` parsed with `rsplit(":", 1)`).
3. Builds the FastAPI app via `create_app(self)` — see below.
4. `await uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, loop="asyncio")).serve()`.

Because `_serve_api` creates the session **before** awaiting `serve()`, by the time uvicorn binds and starts accepting requests the session is already attached to the runner. Endpoint handlers can rely on `runner._session` being non-None.

## The unified FastAPI app

`create_app(runner)` mounts two `APIRouter`s on a single `FastAPI` instance:

### Message router — `POST /message`

```python
@router.post("/message")
async def receive_message(payload: MessagePayload):
    if runner._encapsulator is None:
        raise HTTPException(409, "Protocol not started")
    runner._encapsulator.handle_packet(payload.message)
    return {"status": "ok"}
```

The 409 guard prevents `AttributeError` if a peer sends a message before this node has finished `/protocol/start`. Wire format and full message semantics: `→ .claude/docs/cross-node-communication.md`.

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
    self._encapsulator = EmbeddedEncapsulator(config, self._loop, self._session)
    self._encapsulator.encapsulate(self._protocol_class)  # instantiate protocol
    self._encapsulator.initialize()                        # call protocol.initialize()

    self._loop.create_task(self._periodic_telemetry())
```

The protocol's `initialize` runs **before** the telemetry loop is started. Note the difference from earlier versions of this runner: there is no longer a separate `_start_message_server` task because the message router is part of the same FastAPI app already running inside `_serve_api`. The `/message` endpoint has been listening since `start_api` started uvicorn — it just rejected with 409 until `_bootstrap_protocol` populated `self._encapsulator`.

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
- **One asyncio loop** owned by `EmbeddedRunner`.
- **No threads.** `uvicorn.Server` runs cooperatively on the same loop (`loop="asyncio"`).
- **Blocking inside any `handle_*` hook freezes everything** — including the `/protocol` and `/message` endpoints.

## Related docs

- `→ .claude/docs/configuration.md` — what `RunnerConfiguration` fields control and their invariants.
- `→ .claude/docs/encapsulator-interface.md` — how `EmbeddedEncapsulator` and `EmbeddedProvider` translate calls once the loop is running.
- `→ .claude/docs/protocol-interface.md` — how the five `IProtocol` hooks are invoked from this loop.
- `→ .claude/docs/mobility-and-telemetry.md` — coordinate frames and the telemetry fetch path.
- `→ .claude/docs/cross-node-communication.md` — the `/message` router and peer HTTP.

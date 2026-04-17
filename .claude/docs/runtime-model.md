# Runtime Model

How `EmbeddedRunner` boots, runs, and shuts down. Source: `gradys_embedded/runner/runner.py`.

## The two-phase entry point

```python
runner = EmbeddedRunner(config, MyProtocol)
ok = runner.setup()    # phase 1 — synchronous, arms & takes off
if ok:
    runner.run()        # phase 2 — blocking event loop
```

`setup()` and `run()` are split so the caller can abort if the drone refuses to arm or take off, before any protocol code executes. Do not call `run()` without calling `setup()` first — the runner sets `_ready_to_run = False` by default and logs a fatal message if `run()` is reached without setup.

### `setup()` — `_goto_initial_position`

Synchronous (runs an event loop to completion via `run_until_complete`). Three HTTP calls against `http://localhost:<uav_api_port>`:

1. `GET /command/arm` — arms the flight controller. Any non-200 aborts setup.
2. `GET /command/takeoff?alt=<initial_position[2]>` — takes off to the configured altitude.
3. `POST /movement/go_to_gps_wait` — converts `initial_position` (cartesian NEU, meters) to GPS via `cartesian_to_geo(origin_gps_coordinates, initial_position)` and **waits for the drone to arrive** before returning.

Returns `True` if all three succeed, `False` on any failure (errors logged). Creates a single `aiohttp.ClientSession` reused for the rest of the session.

### `run()` — blocking

Wires the runner into the asyncio loop and calls `run_forever()`:

1. `logging.basicConfig(level=INFO, ...)` — enables INFO-level logging.
2. `self._loop.create_task(self._bootstrap())` — schedules the bootstrap coroutine.
3. `self._loop.run_forever()` — blocks until `KeyboardInterrupt` (Ctrl-C) or an unhandled exception propagates out.
4. On exit: `encapsulator.finish()` → close aiohttp session → close loop.

## `_bootstrap` — what happens on the first loop tick

```python
async def _bootstrap(self):
    self._encapsulator = EmbeddedEncapsulator(config, loop, session)
    self._encapsulator.encapsulate(self._protocol_class)  # instantiate protocol
    self._encapsulator.initialize()                        # call protocol.initialize()

    self._loop.create_task(self._start_message_server())
    self._loop.create_task(self._periodic_telemetry())
```

The protocol's `initialize` runs **before** the message server or telemetry loop is started. That means:

- Timers scheduled in `initialize` fire correctly (the loop is running).
- Messages sent from `initialize` via `BroadcastMessageCommand` work because the **sender** side uses the shared aiohttp session — but receivers whose message server hasn't started yet (boot ordering across drones is uncoordinated) may drop the message. Guard early sends with a timer delay if ordering matters.

## Message server — `_start_message_server`

Uses its own `node_ip_dict[node_id]` entry to pick the listening port. Parses `"host:port"` with `rsplit(":", 1)`, converts the port to int, and serves a `FastAPI` app with a single `POST /message` endpoint on `0.0.0.0:<port>` via `uvicorn.Server(..., loop="asyncio")`.

The server runs on the same asyncio loop as the protocol — it does not spawn a thread. Blocking inside `handle_packet` blocks the whole node (timers, telemetry, outgoing HTTP). Details of the message contract: `→ .claude/docs/cross-node-communication.md` which covers the `/message` endpoint payload, SEND/BROADCAST routing, and failure modes.

## Telemetry loop — `_periodic_telemetry`

Infinite coroutine:

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

Conversion details and the NEU convention: `→ .claude/docs/mobility-and-telemetry.md` which covers `geo_to_cartesian`, `cartesian_to_geo`, and the endpoint map.

## Shutdown

The runner's `finally` block in `run()`:

1. Calls `encapsulator.finish()` — the protocol's `finish()` runs. At this point the loop is about to close, so `finish()` cannot rely on async operations or peer HTTP calls resolving.
2. Closes the aiohttp session (`run_until_complete(session.close())`).
3. Closes the event loop.

**Do not issue outbound communication commands from `finish()`** — they schedule aiohttp tasks on a loop that is closing. Log local state, write files, but do not expect anything to reach the wire.

`KeyboardInterrupt` (Ctrl-C) is the intended shutdown path. SIGTERM reaches the same `finally` because `run_forever()` raises on signal. Uncaught exceptions inside tasks do **not** stop the loop — they just log — unless they come from inside `run_forever`'s machinery.

## Threading / concurrency model summary

- **One process per drone.**
- **One asyncio loop** owned by `EmbeddedRunner`.
- **No threads.** `uvicorn.Server` runs cooperatively on the same loop (`loop="asyncio"`).
- **Blocking inside any `handle_*` hook freezes everything.** Use `loop.create_task` for fire-and-forget work the protocol itself wants to do async.

## Related docs

- `→ .claude/docs/configuration.md` — what `RunnerConfiguration` fields control and their invariants (`origin_gps_coordinates`, `node_ip_dict`, etc.).
- `→ .claude/docs/encapsulator-interface.md` — how `EmbeddedEncapsulator` and `EmbeddedProvider` translate calls once the loop is running.
- `→ .claude/docs/protocol-interface.md` — how the five `IProtocol` hooks are invoked from this loop; for the authoritative interface spec see the cross-project pointer inside that file.
- `→ .claude/docs/mobility-and-telemetry.md` — coordinate frames and the telemetry fetch path.
- `→ .claude/docs/cross-node-communication.md` — message server and peer HTTP.

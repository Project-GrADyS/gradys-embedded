# Runtime Model

How the long-running mission service boots, runs missions, and shuts down. Sources: `gradys_embedded/runner/runner.py` (`EmbeddedRunner`), `gradys_embedded/runner/mission.py` (`MissionManager`), `gradys_embedded/runner/control_panel.py` (control app).

## The single public entry point

```python
runner = EmbeddedRunner(config)   # provisioned RunnerConfiguration only — no protocol class
runner.start_api()                # blocks; owns the asyncio loop
```

`start_api()` boots the service **idle**. It does not arm the drone, bind the data plane, or instantiate any protocol — there is no default protocol at all. Everything after boot is driven over HTTP against the control plane; the protocol and the data-plane transport arrive with each mission:

```
control plane  (control_api_port, always plain HTTP, bound at boot):
  /protocols   upload / list / delete protocol modules
  /mission     load / setup / start / stop / reset / status
  /runs        list / manifest / file / archive / delete run data

data plane     (data_port, transport = the mission's communication_protocol,
                bound at /mission/load — an idle drone listens on nothing else):
  POST /message   inter-node delivery for http/https/http3 (409 until /mission/start)
  (zenoh)         inter-node delivery via Zenoh pub/sub instead of POST /message
```

## `start_api()` — what boot does

1. `logging.basicConfig(level=INFO, ...)`.
2. `self._loop = asyncio.new_event_loop()` — the runner owns the loop.
3. Schedules `_serve_communication()` as a task (with a done-callback so a boot failure stops the loop instead of dying as an unretrieved task exception).
4. `self._loop.run_forever()` — blocks until `KeyboardInterrupt`.
5. `finally`: `_shutdown()` (below).

`_serve_communication()`:

1. Creates `self._session = aiohttp.ClientSession()` on the runner-owned loop. Every subsequent HTTP call (setup, telemetry, HTTP peer sends) reuses it.
2. Creates `self.mission = MissionManager(self)` — the owner of everything per-mission (state machine, run directory, protocol resolution, resource monitor). The runner keeps only process-scoped state.
3. Starts `_periodic_telemetry()` as a **process-lifetime** task (see below).
4. Serves the control app (`create_control_app(self)`) on `control_api_port` via uvicorn — the only server at boot. Its per-server signal handlers are disabled; the runner's own `KeyboardInterrupt` path owns shutdown.

Boot no longer touches `uav_api`: the coordinate frame is a mission parameter and is resolved at load time (`resolve_frame`), so the service comes up even when `uav_api` is not yet running.

## The mission state machine

`MissionManager` runs one mission at a time:

```
IDLE ──load──▶ LOADED ──setup──▶ READY ──start──▶ RUNNING ──stop──▶ RETURNING ──▶ IDLE
                                                                  (landing detected)
        any non-IDLE state ──reset──▶ IDLE   (escape hatch; does NOT command the vehicle)
```

`setup` and `start` are deliberately separate: fleet coordination needs every drone at its initial position before any protocol begins. Rejected transitions raise `MissionError`, which the control app maps to its HTTP status (409 by default). `GET /mission/status` reports the state, the run id, live `tracked_variables`, and the mission's effective frame/peer-map/transport — echoed so the mission layer can confirm every drone agrees before starting.

### `POST /mission/load` — validate, bind the data plane, open the run

Requires IDLE (409 otherwise). Every fallible step runs **before** anything is created, so a failed load leaves no orphan run directory, stale log handler, or half-set state:

1. **Build and validate the `MissionConfiguration`** from the request body — 400 on an unknown transport, a missing pip extra, `zenoh_quic` without a provisioned certfile, a missing `node_ip_dict` (required unless zenoh + `auto_scout`; zenoh without `auto_scout` also requires the node's own entry), or any dataclass `ValueError`.
2. **Check disk space** — 507 below `min_free_disk_mb`.
3. **Resolve the protocol** (`resolve_protocol`): import `module` or `module:ClassName` from `protocols_dir`; 400 on import failure or ambiguity. A cached module is reloaded so a re-uploaded protocol runs its new code.
4. **Resolve the frame** (`runner.resolve_frame`): fill any omitted `origin_gps_coordinates`/`x_axis_degrees` from this drone's own telemetry, with a loud warning (a fleet relying on this has every drone in a different frame). 502 if `uav_api` is unreachable. Done at load rather than setup so `/mission/status` reports a concrete frame straight away.
5. **Bind the data plane** (`runner.start_backend(context)`): build the mission transport's `CommunicationBackend` and run `backend.serve()` as a task. `start_backend` then awaits a **readiness handshake** — it races the serve task's death against `backend.wait_ready()` with a 10 s timeout (`BACKEND_START_TIMEOUT`), so a port conflict or bad TLS material fails the load with 500 instead of returning 200 with a dead data plane. A no-op when the running backend already matches the mission's signature (transport + TLS material; zenoh also keys on `auto_scout` + the peer map) — consecutive missions on the same transport reuse the listener; a different transport releases and rebinds the same `data_port`. Binding at load rather than start means every drone is listening before any of them sends: the fleet loads together, then starts together.
6. **Commit**: create the run directory (`run_<timestamp>[_label]`), attach the per-run log handler, write `RUN_INFO.json` (outcome `"loaded"`), set state LOADED. Disk trouble mid-commit rolls all of it back.

### `POST /mission/setup` — arm, take off, fly to position

Requires LOADED (409; 400 if the mission was loaded without an `initial_position`). Awaits `runner.goto_initial_position`: three calls against `http://localhost:<uav_api_port>` —

1. `GET /command/arm`
2. `GET /command/takeoff?alt=<initial_position[2]>`
3. `POST /movement/go_to_gps_wait` with `cartesian_to_geo(origin, initial_position, x_axis_degrees)` — **blocks until arrival**.

Any non-200 aborts with 500 and the state stays LOADED, so setup is retryable. On success: READY.

### `POST /mission/start` — run the protocol

Requires READY (409). Starts the resource monitor, then `runner.bootstrap_protocol`: builds an `EmbeddedEncapsulator` around the mission context and the already-bound backend, instantiates the protocol, binds it as `runner._encapsulator`, and calls `protocol.initialize()`. State: RUNNING.

The backend is untouched here — **both transports read `runner._encapsulator` per delivery, so rebinding it re-routes inbound messages atomically without rebinding any port** (this is the swap seam that lets protocols change between missions without restarting the process).

### `POST /mission/stop` — finish, flush, RTL

Allowed from RUNNING, READY, or LOADED. Order matters:

1. `runner.teardown_protocol()` — `encapsulator.finish()` runs the protocol's `finish()` (data flush) and gates the provider off, so a stopped protocol cannot fight the return or command a disarmed vehicle. `runner._encapsulator` becomes `None` again (inbound messages go back to 409/drop).
2. Stop the resource monitor.
3. If the vehicle was flying (RUNNING/READY): `request_return_to_launch()` **fire-and-forget** — `/command/rtl` blocks until home *and* disarmed (~250 s), so stop returns while the drone is still descending. State: RETURNING. Otherwise (LOADED): straight to IDLE, run finalized.

RETURNING → IDLE is decided locally by the telemetry loop: when `relative_alt <= telemetry_landed_alt`, `mission.note_landed()` finalizes the run with outcome `"completed"`.

### `POST /mission/reset` — the escape hatch

Allowed from any non-IDLE state (409 when idle). Forces the state machine back to IDLE: tears down the protocol if one is live, finalizes the run with outcome `"reset"`, and **does NOT command the vehicle** — an RTL already issued keeps flying; an airborne vehicle stays airborne. Exists because the only ordinary exit from RETURNING is the telemetry loop's landing detection, and a broken poll must not strand the service until a field power-cycle.

## The inbound-message guard

The `/message` route (HTTP transports) rejects with 409, and the zenoh subscriber callback drops the sample, while `runner._encapsulator is None` — i.e. any time before `/mission/start` and after stop/reset. The data plane itself only exists from `/mission/load` onward; before a load there is nothing bound on `data_port` at all, so a peer's send fails at the connection level instead.

Practical consequence: messages broadcast from `initialize` reach peers that have already started, and are dropped by peers still in setup. Start ordering across drones is uncoordinated — design protocols to tolerate dropped bootstrap broadcasts, or delay the first broadcast with a short timer.

## Telemetry loop — `_periodic_telemetry`

Process-lifetime, started at boot — not per mission. Each tick re-reads the active mission context, so it follows a load/swap on its own:

- **Idle** (no mission): polls at `EmbeddedRunner.IDLE_TELEMETRY_INTERVAL` (0.5 s), performs **no frame conversion** and delivers nothing to any protocol — it is the uav_api health signal and the landing detector.
- **During a mission**: polls at the mission's `telemetry_interval`, converts `(lat, lon, relative_alt)` with the **mission's** frame (`geo_to_cartesian(origin, geo, x_axis_degrees)`), and calls `encapsulator.handle_telemetry(...)` when a protocol is live.
- Every tick also runs `_check_for_landing`, which flips a RETURNING mission to IDLE once `relative_alt <= telemetry_landed_alt`.

Key points:

- **The altitude used is `relative_alt`** (meters above takeoff), aligning z with the cartesian frame.
- **A failed fetch is swallowed** and retried next interval; the protocol gets no explicit stall signal.
- **`handle_telemetry` runs on the asyncio loop** — blocking inside it piles up everything else.

Conversion details: `→ .claude/docs/mobility-and-telemetry.md`.

## Shutdown

`start_api()`'s `finally` block runs `_shutdown()`:

1. `mission.shutdown()` — tears down any live protocol (its `finish()` runs, data flushes), writes outcome `"interrupted"` if a mission was in progress, detaches the run log. Deliberately does **not** command the vehicle: a process shutdown is not a mission stop.
2. Cancels the telemetry task.
3. `stop_backend()` — asks the data plane to close so the listener is released in an orderly way (not just cancelled), with a 5 s timeout (`BACKEND_STOP_TIMEOUT`) so a wedged transport cannot hang shutdown.
4. Cancels remaining tasks, closes the aiohttp session, closes the loop.

**Do not issue outbound communication commands from `finish()`** — they schedule tasks on a loop that is closing. `KeyboardInterrupt` (Ctrl-C) is the intended shutdown path.

## Threading / concurrency model summary

- **One process per drone**, one asyncio loop owned by `EmbeddedRunner`.
- **Control plane always; data plane per mission.** Both servers run cooperatively on the same loop (uvicorn/Hypercorn with `loop="asyncio"`, per-server signal handlers disabled); no threads for HTTP.
- **`MissionManager` owns per-mission state**, the runner owns process state — the split is what makes protocol swap-without-restart possible.
- **Exception: Zenoh.** Its subscriber callback fires on a Zenoh-owned background thread and only marshals onto the loop via `call_soon_threadsafe` — it must never touch protocol/loop state directly.
- **Blocking inside any `handle_*` hook freezes everything** — the control endpoints, `/message`, and the telemetry loop included.

## Related docs

- `→ .claude/docs/configuration.md` — the provisioned/mission config split and every field's semantics.
- `→ .claude/docs/encapsulator-interface.md` — how `EmbeddedEncapsulator` and `EmbeddedProvider` translate calls once a protocol runs.
- `→ .claude/docs/protocol-interface.md` — how the five `IProtocol` hooks are invoked from this loop.
- `→ .claude/docs/mobility-and-telemetry.md` — coordinate frames and the telemetry fetch path.
- `→ .claude/docs/cross-node-communication.md` — the data-plane transports and peer messaging.

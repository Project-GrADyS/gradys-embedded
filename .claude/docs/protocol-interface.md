# Protocol Interface (Embedded Implementation Notes)

**This doc is implementation-only.** The authoritative definition of `IProtocol`, `IProvider`, `instantiate`, the five lifecycle hooks, and `tracked_variables` lives in the simulator — see `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/protocol-lifecycle.md` which is the ecosystem's source of truth for the interface. This project's copy of `interface.py` mirrors that spec.

What follows is the **embedded-specific invocation story**: who calls each hook, when, on what thread, and what breaks if you violate asyncio's rules.

## Where each hook comes from

| Hook | Trigger | Caller | Thread/loop |
|---|---|---|---|
| `initialize()` | Once, during `_bootstrap_protocol()` triggered by `POST /protocol/start` | `EmbeddedEncapsulator.initialize()` | asyncio loop |
| `handle_timer(timer)` | `loop.call_at(timestamp, ...)` fires | `EmbeddedProvider._on_timer` → `self.handle_timer` callback | asyncio loop |
| `handle_packet(message)` | HTTP `POST /message` arrives (after `/protocol/start`) | FastAPI route → `encapsulator.handle_packet` | asyncio loop (uvicorn on the same loop) |
| `handle_telemetry(telemetry)` | `_periodic_telemetry` coroutine ticks | `_periodic_telemetry` → `encapsulator.handle_telemetry` | asyncio loop |
| `finish()` | Runner shutdown (Ctrl-C or exception) | `EmbeddedRunner.start_api()`'s `finally` block | asyncio loop, loop about to close |

**Every hook runs on the single asyncio loop.** There is no thread boundary. Any blocking call inside a hook freezes the message server, timer dispatch, telemetry polling, and outbound HTTP simultaneously.

## `initialize()` — one extra constraint vs. simulation

In the simulator, `initialize` runs before any events fire. Here, it runs on the asyncio loop during the `POST /protocol/start` handler, just after the encapsulator is wired and **before** the telemetry loop ticks. The `/message` endpoint has already been serving on this node since `start_api()` launched uvicorn — but it returned 409 until `_bootstrap_protocol` populated `runner._encapsulator`.

Consequences:

- Timers scheduled in `initialize` will fire correctly; the loop is already running when `call_at` is set.
- A message broadcast from `initialize` will be sent (the aiohttp session and `node_ip_dict` are both ready), but **peers may not have started yet**. Peers still waiting on their own `/protocol/start` reject incoming messages with 409. Design protocols to tolerate missed bootstrap broadcasts, or delay the first broadcast with a short timer.
- You can safely read `self.provider.get_id()` and `self.provider.current_time()`.

## `handle_timer` — asyncio scheduling, not simulator scheduling

`self.provider.schedule_timer(timer, timestamp)` calls `loop.call_at(timestamp, self._on_timer, timer)`. Consequences:

- **`timestamp` must be an absolute loop time**, not a delta. Always compute `self.provider.current_time() + delta` where `current_time()` returns `loop.time()`.
- **Monotonic clock only.** `current_time()` starts at some arbitrary value (typically large) and strictly increases. Protocols must never compare `current_time()` across nodes — it is a per-process monotonic clock.
- `loop.call_at` with a past timestamp fires immediately on the next tick. There is no `EventLoopException` guard as in the simulator's `EventLoop`.
- `cancel_timer(timer)` cancels the `TimerHandle` stored under that tag. If the same tag was used for multiple concurrent timers, **only the last-registered one is cancelled** — `_timers` is a `dict[str, TimerHandle]` keyed by tag and each new `schedule_timer` overwrites the previous entry without cancelling it. This differs from the simulator, where `cancel_timer` removes all pending tags. Avoid reusing a tag while an earlier timer with that tag is still pending.

## `handle_packet` — FastAPI-driven

Triggered by `POST /message` with JSON body `{"message": str, "source": int}`. The encapsulator forwards only `payload.message` to the protocol — the `source` field is discarded. **If your protocol needs the sender id, embed it inside the message** (every showcase that tracks senders does this).

The FastAPI route is `async def`, so it yields back to the loop after calling `handle_packet`. Still, the protocol's `handle_packet` runs inline within the HTTP request handler — a slow `handle_packet` stalls the HTTP response and backs up uvicorn's request queue.

## `handle_telemetry` — polled

Called every `telemetry_interval` seconds (default 0.5 s). The telemetry object carries `current_position` in cartesian NEU meters relative to `origin_gps_coordinates`. There is no velocity or orientation field today — if a protocol needs them, extend `Telemetry` and the runner's fetch path together.

Network latency to `uav_api` adds a (usually small) delay to this hook's invocation. Do not assume `handle_telemetry` ticks at exactly `telemetry_interval` — budget slack for waypoint-arrival tolerance.

## `finish()` — late in shutdown

Called from the runner's `finally` block just before the aiohttp session closes. The loop is about to stop. Do **not** issue outbound mobility or communication commands here — they schedule aiohttp tasks on a loop that is tearing down, and the tasks typically never complete. Use `finish()` for:

- Local logging / writing tracked variables to disk.
- Last-resort state preservation.

If you need a graceful shutdown broadcast, do it via a timer that fires before you Ctrl-C, not from `finish()`.

## `IProvider` methods — how they map

Every provider method on the embedded side has a concrete transport:

| Method | Transport |
|---|---|
| `send_communication_command(SEND)` | `aiohttp.post http://<dest_ip:port>/message` — fire-and-forget |
| `send_communication_command(BROADCAST)` | Iterates `node_ip_dict`, POSTs to each peer — fire-and-forget |
| `send_mobility_command(GOTO_COORDS)` | Converts NEU→GPS via `cartesian_to_geo`, POSTs `/movement/go_to_gps` on `localhost:uav_api_port` |
| `send_mobility_command(GOTO_GEO_COORDS)` | POSTs `/movement/go_to_gps/` directly |
| `send_mobility_command(SET_SPEED)` | GETs `/command/set_air_speed?new_v=<int>` |
| `schedule_timer(timer, timestamp)` | `loop.call_at(timestamp, _on_timer, timer)` |
| `cancel_timer(timer)` | Cancels the `TimerHandle` stored for that tag |
| `current_time()` | `loop.time()` |
| `get_id()` | `config.node_id` |
| `tracked_variables` | Bare `dict` — no side effects; pure state. |

Full details with HTTP shapes:

- `→ .claude/docs/cross-node-communication.md` — `/message` endpoint, SEND vs BROADCAST, fire-and-forget failure modes.
- `→ .claude/docs/mobility-and-telemetry.md` — NEU↔GPS conversion, mobility endpoint map, telemetry polling.
- `→ .claude/docs/encapsulator-interface.md` — encapsulator/provider delegation and timer lifecycle.

Endpoint contracts for `uav_api` live at `→ /home/fleury/gradys/major_projects/uav_api/.claude/docs/specification.md` which is the authoritative HTTP spec.

## Portability checklist

A protocol is portable between simulator and hardware if and only if it uses exclusively:

- `self.provider.send_communication_command(...)`
- `self.provider.send_mobility_command(...)`
- `self.provider.schedule_timer(...)` / `cancel_timer(...)`
- `self.provider.current_time()`
- `self.provider.get_id()`
- `self.provider.tracked_variables`

Direct imports from `gradysim.simulator.*` (handlers, simulation builder) break portability — that code is simulator-only.

## Related docs

- `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/protocol-lifecycle.md` — **authoritative** IProtocol/IProvider spec.
- `→ .claude/docs/runtime-model.md` — the asyncio loop that invokes these hooks.
- `→ .claude/docs/encapsulator-interface.md` — how the encapsulator delegates each hook.
- `→ .claude/docs/plugins-and-extensions.md` — dispatcher pattern for composing protocol behavior without subclassing.

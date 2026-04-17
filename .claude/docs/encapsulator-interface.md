# Encapsulator Interface

The delegation layer between `EmbeddedRunner` and the protocol. Sources: `gradys_embedded/encapsulator/interface.py` (`IEncapsulator` abstract base) and `gradys_embedded/encapsulator/embedded.py` (`EmbeddedEncapsulator`, `EmbeddedProvider`).

## IEncapsulator — the abstract contract

```python
class IEncapsulator(ABC, Generic[T]):
    protocol: T   # the wrapped protocol instance

    @abstractmethod
    def encapsulate(self, protocol: Type[T]) -> None: ...
    @abstractmethod
    def initialize(self) -> None: ...
    @abstractmethod
    def handle_timer(self, timer: str) -> None: ...
    @abstractmethod
    def handle_packet(self, message: str) -> None: ...
    @abstractmethod
    def handle_telemetry(self, telemetry: Telemetry) -> None: ...
    @abstractmethod
    def finish(self) -> None: ...
```

Identical shape to the simulator's `PythonEncapsulator` — the same protocol class, encapsulated in either project, exposes the same five hooks. The **only** embedded-specific extension is the `EmbeddedProvider` it constructs.

## EmbeddedEncapsulator — the glue

```python
class EmbeddedEncapsulator(IEncapsulator):
    def __init__(self, runner_configuration, loop, session):
        self.provider = EmbeddedProvider(
            runner_configuration, loop, self.handle_timer, session,
        )

    def encapsulate(self, protocol: Type[IProtocol]) -> None:
        self.protocol = protocol.instantiate(self.provider)
        self.provider.set_timer_callback(self.handle_timer)

    def initialize(self):         self.protocol.initialize()
    def handle_timer(self, t):    self.protocol.handle_timer(t)
    def handle_packet(self, m):   self.protocol.handle_packet(m)
    def handle_telemetry(self, t): self.protocol.handle_telemetry(t)
    def finish(self):             self.protocol.finish()
```

Every hook is a thin pass-through. The encapsulator does **not** add logging, instrumentation, or exception handling — whatever the protocol raises propagates up to the caller (usually the asyncio loop, which logs and continues for task exceptions).

### Bootstrap timing — the timer callback quirk

The provider takes a `timer_callback` in its constructor. The encapsulator passes `self.handle_timer` as that callback. **But `self.handle_timer` refers to `self.protocol.handle_timer` only after `encapsulate()` has run** — the `__init__` assignment captures a bound method whose body reads `self.protocol`, which is not set until `encapsulate` assigns it.

That is why `encapsulate` ends with `self.provider.set_timer_callback(self.handle_timer)` — it re-binds the callback after the protocol exists. If you modify this code path, preserve that order: construct provider → `encapsulate` sets `self.protocol` → re-bind timer callback.

## EmbeddedProvider — how each IProvider method is implemented

### Constructor — what it captures

```python
def __init__(self, runner_configuration, loop, timer_callback, session):
    self.node_id = runner_configuration.node_id
    self.node_ip_dict = runner_configuration.node_ip_dict
    self.origin_gps_coordinates = runner_configuration.origin_gps_coordinates
    self._timer_callback = timer_callback
    self._session = session              # shared aiohttp ClientSession
    self.tracked_variables = {}          # plain dict
    self._loop = loop                    # asyncio loop owned by EmbeddedRunner
    self._uav_base_url = f"http://localhost:{runner_configuration.uav_api_port}"
    self._timers: dict[str, asyncio.TimerHandle] = {}
```

The provider never owns the loop or the session — both are handed in by `EmbeddedRunner` so shutdown is centralized.

### Fire-and-forget helper

```python
def _fire_and_forget(self, coro) -> None:
    task = self._loop.create_task(coro)
    task.add_done_callback(self._log_task_exception)
```

Used for every outbound HTTP call (communication and mobility). The calling protocol method returns immediately; any exception inside the task is logged but does not propagate. Details of the consequences for delivery guarantees: `→ .claude/docs/cross-node-communication.md` which covers peer HTTP, and `→ .claude/docs/mobility-and-telemetry.md` which covers mobility endpoints.

### Timer scheduling — asyncio, not a heap

```python
def schedule_timer(self, timer: str, timestamp: float) -> None:
    handle = self._loop.call_at(timestamp, self._on_timer, timer)
    self._timers[timer] = handle

def _on_timer(self, timer: str) -> None:
    self._timers.pop(timer, None)
    if self._timer_callback is not None:
        self._timer_callback(timer)
```

Three consequences, each subtly different from the simulator:

1. **`timestamp` is absolute `loop.time()`.** Use `self.provider.current_time() + delta`. `loop.call_at` with a past timestamp fires on the next tick — there is no `EventLoopException`.
2. **Same-tag overwrites are silent.** `self._timers[timer] = handle` — if a timer with that tag was already pending, the old `TimerHandle` is overwritten without being cancelled. The old timer will still fire at its original time. This differs from the simulator's semantics. If your protocol relies on "only one pending timer per tag", cancel before re-scheduling.
3. **`cancel_timer(timer)` only cancels the one stored in `_timers`.** If multiple same-tag timers are pending (because you overwrote without cancelling), only the most recent handle is cancelled — the others will still fire.

Implementation-level protocol gotchas listed in full at `→ .claude/docs/protocol-interface.md` which contrasts the embedded hook invocation with the simulator's.

### `current_time()` — monotonic

```python
def current_time(self) -> float:
    return self._loop.time()
```

`loop.time()` is a monotonic clock scoped to this process. It is **not** GPS time, UTC, or epoch seconds. Never compare `current_time()` values across nodes — use GPS from `/telemetry/gps` if you need cross-node timing.

### `tracked_variables`

A plain `dict[str, Any]`. No callback, no dispatch. Protocols write to it freely; no tool currently reads it on hardware. Use it for protocol-internal state that you also want to inspect post-flight via logging.

### `close()`

```python
async def close(self) -> None:
    if self._session is not None and not self._session.closed:
        await self._session.close()
```

Unused — the runner closes the session itself in its `finally` block. Safe to call if a custom runner wants to manage the provider's lifetime directly.

## Writing a custom encapsulator

Very rarely needed. The two reasons you might:

1. **Testing** — wrap the protocol with a mock provider that records all calls for assertion.
2. **A new execution mode** — e.g., hardware with a non-HTTP transport. In that case, clone `EmbeddedEncapsulator`, swap `EmbeddedProvider` for one that speaks your transport, and reuse the `IEncapsulator` interface.

Keep the provider methods side-effect free relative to the protocol — the protocol should not observe a difference other than the effect of commands on the physical world.

## Related docs

- `→ .claude/docs/runtime-model.md` — when `encapsulate` and `initialize` are called relative to the asyncio loop.
- `→ .claude/docs/protocol-interface.md` — how the five hooks are invoked (authoritative spec pointer inside).
- `→ .claude/docs/mobility-and-telemetry.md` — what `send_mobility_command` hits on `uav_api`.
- `→ .claude/docs/cross-node-communication.md` — what `send_communication_command` hits on peers.
- `→ .claude/docs/configuration.md` — what the provider reads out of `RunnerConfiguration`.

# Cross-Node Communication

How protocols send and receive messages on real hardware. Each drone runs a FastAPI server with a single `/message` endpoint; messages are plain HTTP POSTs between drones over whatever IP network the fleet is on.

Sources: `gradys_embedded/runner/message_api.py` (server), `gradys_embedded/encapsulator/embedded.py` (`EmbeddedProvider.send_communication_command`, sender).

## The `/message` endpoint — what every node exposes

Defined in the message router built by `message_api.create_app(runner)`:

```python
class MessagePayload(BaseModel):
    message: str
    source: int

@router.post("/message")
async def receive_message(payload: MessagePayload):
    if runner._encapsulator is None:
        raise HTTPException(409, "Protocol not started")
    runner._encapsulator.handle_packet(payload.message)
    return {"status": "ok"}
```

Wire format: JSON `{"message": "<payload string>", "source": <sender_node_id>}`. Content-Type `application/json`.

The receiver's FastAPI route hands `payload.message` to the encapsulator, which calls the protocol's `handle_packet`. **The `source` field is discarded** — if your protocol needs the sender id, embed it in the message body (every example in `examples/simple/protocol.py` does this).

The server listens on `0.0.0.0:<port>` where `<port>` comes from `node_ip_dict[node_id]` for its own id. The same FastAPI app also exposes the `/protocol/setup` and `/protocol/start` endpoints (`→ .claude/docs/runtime-model.md`). It shares the asyncio loop with the rest of the runner — there is no separate thread.

## Transport — http (default) vs https vs http3

The message API runs over one of three transports, selected by `RunnerConfiguration.communication_protocol` (default `"http"`). The FastAPI app, the `/message` route, and the wire payload are **identical** in every mode; only the server and client transport change. `communication_protocol` must be the same on every node.

| | `"http"` (default) | `"https"` | `"http3"` |
|---|---|---|---|
| Server | uvicorn, HTTP/1.1 over TCP | uvicorn, HTTP/1.1 over TLS (TCP) | Hypercorn `quic_bind`, HTTP/3 over QUIC (UDP), TLS 1.3 |
| Peer URL scheme | `http://<addr>/message` | `https://<addr>/message` | `https://<addr>/message` |
| Client | shared `aiohttp.ClientSession` | shared `aiohttp.ClientSession` (per-request `ssl=`) | lazily-created `niquests.AsyncSession` |
| TLS | none | TLS 1.2/1.3 | TLS 1.3 (QUIC-mandated) |
| Deps | core install | core install | `pip install "gradys-embedded[http3]"` |

`"https"` and `"http3"` both require the server to present a certificate. If `certfile`/`keyfile` are set in the configuration, the server binds with them and the client **verifies peers against `certfile`**. If they are omitted, the server binds with an **ephemeral self-signed certificate generated at boot** (temp file, not persisted) and the client disables verification (`ssl=False` for https, `verify=False` for http3) — the channel is still encrypted, but peers are not authenticated. See `→ .claude/docs/configuration.md`.

The local `uav_api` connection is unaffected by `communication_protocol`; it always uses plain HTTP on `localhost`.

**Until `/protocol/start` has succeeded, `/message` returns 409.** uvicorn binds the port from the moment `start_api()` runs, but the encapsulator that owns `handle_packet` only exists after start. Peers that broadcast during their own `initialize` may see this 409 on receivers that have not started yet.

## Sending — SEND

```python
self.provider.send_communication_command(
    SendMessageCommand(message="hello", destination=3)
)
```

The provider:

1. Looks up `node_ip_dict[3]` → `"192.168.1.12:5000"` (for example).
2. Fire-and-forgets a POST to `http://192.168.1.12:5000/message` (or `https://` under `"https"`/`"http3"`) with body `{"message": "hello", "source": <my_id>}`, via `_send_to_peer`.

Failure modes:

- **Unknown destination** — `node_ip_dict.get(destination)` returns `None`; the provider logs `"Unknown destination node <id>"` and drops the message. No exception reaches the protocol.
- **Peer unreachable / timeout / connection refused** — the aiohttp task fails; `_log_task_exception` logs the error. The protocol is never notified.
- **Peer returns non-200** — the provider logs `"POST <url> returned <status>: <body>"`. The protocol is never notified.

**The protocol never learns about delivery outcomes.** Design all peer interactions as unreliable. If you need acknowledgments, implement them at the protocol level (e.g., expect an ACK message within a timer window; resend on timeout).

## Sending — BROADCAST

```python
self.provider.send_communication_command(
    BroadcastMessageCommand(message="heartbeat")
)
```

The provider iterates `node_ip_dict.items()` and POSTs to every entry whose id is not its own. Same fire-and-forget semantics per peer; a single slow peer does not block the others because each POST is its own asyncio task.

**Broadcast is O(n) HTTP calls**, not a multicast. Scaling beyond a handful of drones means handling n² traffic on the network. For dense topologies, implement a relay or gossip pattern at the protocol layer rather than broadcasting blindly.

## Fire-and-forget — what it really means

`EmbeddedProvider._fire_and_forget`:

```python
def _fire_and_forget(self, coro) -> None:
    task = self._loop.create_task(coro)
    task.add_done_callback(self._log_task_exception)
```

- The call to `send_communication_command` returns immediately. Your protocol does not wait for the HTTP request to be sent, let alone acknowledged.
- A single asyncio task per send; aiohttp reuses the shared `ClientSession` connection pool.
- Exceptions inside the task are logged via the done callback but never propagate.
- There is **no retry**. A one-shot failure is a lost message.

## Peer discovery — there isn't any

`node_ip_dict` is static configuration. If a new drone joins the fleet mid-flight, existing drones do not know about it. If a drone's IP changes (DHCP lease expires, switches networks), every other drone's `node_ip_dict` is now wrong and messages to it drop silently.

**Pin IPs.** Use static leases, a dedicated ad-hoc/mesh network, or a private LAN with reserved addresses. Do not rely on DHCP for fleet deployment.

## Latency and ordering

- **Latency depends on your network.** LAN: sub-millisecond. Wi-Fi mesh at distance: tens to hundreds of ms. 4G/5G backhaul: 50–200 ms typical.
- **Ordering is not guaranteed.** Two SEND commands to the same peer become two independent asyncio tasks; whichever aiohttp finishes the TCP handshake first wins. If order matters, sequence-number your messages at the protocol level.
- **Receiver blocks on `handle_packet`.** The FastAPI route runs `encapsulator.handle_packet(...)` synchronously. A slow protocol handler backs up uvicorn's request queue and delays subsequent incoming messages on that node.

## Security model — there isn't one

`/message` has no authentication and no rate limiting. In the default `"http"` mode there is no TLS either (plain HTTP). The `"https"` and `"http3"` modes encrypt the channel with TLS, but with the default ephemeral cert they do **not** authenticate peers (client verification disabled) — set a shared `certfile`/`keyfile` across the fleet to get peer authentication. Either way, anyone reachable on the network can POST to `/message` and inject messages. Deploy on a private fleet network.

If you need authentication, wrap the payload in a signed envelope at the protocol level; the transport layer does not help you.

## Debugging communication problems

1. **Is `/message` reachable?** From one drone: `curl -X POST http://<peer>:<port>/message -H 'Content-Type: application/json' -d '{"message":"test","source":0}'` should return `{"status":"ok"}`.
2. **Is `node_ip_dict` right?** A common bug is different drones having drifted copies of `node_ip_dict`. Compare against ground truth on each drone.
3. **Are sends failing silently?** Check each drone's log for `Fire-and-forget task failed:` or `POST ... returned ...` entries. These are the only signal of a send failure.
4. **Is the receiver blocking?** If peer A's `handle_packet` runs slowly, peer B's subsequent sends to A back up. Profile `handle_packet` for hidden blocking calls.

## Related docs

- `→ .claude/docs/runtime-model.md` — when uvicorn (and therefore the `/message` router) starts listening relative to `/protocol/start` and `initialize()`.
- `→ .claude/docs/configuration.md` — `node_ip_dict` shape and shared-origin requirement.
- `→ .claude/docs/protocol-interface.md` — how `handle_packet` is invoked on the asyncio loop.
- `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/messages-and-telemetry.md` — the abstract `CommunicationCommand` types (SEND, BROADCAST).

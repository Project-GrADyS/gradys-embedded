# Cross-Node Communication

How protocols send and receive messages on real hardware. The inter-node **data plane** is selected by `communication_protocol`: for `http`/`https`/`http3` each drone runs a FastAPI server exposing `/message` and peers POST to it; for `zenoh` each drone runs an Eclipse Zenoh peer session and messages are pub/sub by key expression. Either way, this data plane is served on the drone's `node_ip_dict` port and is **separate from the control plane** (`/protocol/setup`, `/protocol/start`), which runs on `control_api_port` (`→ .claude/docs/runtime-model.md`).

Sources: `gradys_embedded/runner/message_api.py` (server apps), `gradys_embedded/runner/runner.py` (`_serve_zenoh` and transport dispatch), `gradys_embedded/encapsulator/embedded.py` (`EmbeddedProvider.send_communication_command`, sender).

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

## Transport — http (default) vs https vs http3 vs zenoh

The data plane runs over one of four transports, selected by `RunnerConfiguration.communication_protocol` (default `"http"`). For the three HTTP transports the FastAPI app, the `/message` route, and the wire payload are **identical**; only the server and client transport change. `communication_protocol` must be the same on every node.

| | `"http"` (default) | `"https"` | `"http3"` | `"zenoh"` |
|---|---|---|---|---|
| Model | request/response, IP-addressed | request/response, IP-addressed | request/response, IP-addressed | **pub/sub, key-addressed, peer (p2p)** |
| Server | uvicorn, HTTP/1.1 over TCP | uvicorn, HTTP/1.1 over TLS (TCP) | Hypercorn `quic_bind`, HTTP/3 over QUIC (UDP), TLS 1.3 | Zenoh peer session + subscribers |
| Addressing | `http://<addr>/message` | `https://<addr>/message` | `https://<addr>/message` | keys `gradys/msg/<dest_id>`, `gradys/msg/broadcast` |
| Client | shared `aiohttp.ClientSession` | shared `aiohttp.ClientSession` (per-request `ssl=`) | lazily-created `niquests.AsyncSession` | shared Zenoh session (`.put`) |
| Discovery | `node_ip_dict` | `node_ip_dict` | `node_ip_dict` | `node_ip_dict` endpoints (`auto_scout=False`) **or** multicast scouting (`auto_scout=True`) |
| Deps | core install | core install | `pip install "gradys-embedded[http3]"` | `pip install "gradys-embedded[zenoh]"` |

`"https"` and `"http3"` both require the server to present a certificate. If `certfile`/`keyfile` are set in the configuration, the server binds with them and the client **verifies peers against `certfile`**. If they are omitted, the server binds with an **ephemeral self-signed certificate generated at boot** (temp file, not persisted) and the client disables verification (`ssl=False` for https, `verify=False` for http3) — the channel is still encrypted, but peers are not authenticated. See `→ .claude/docs/configuration.md`.

The local `uav_api` connection is unaffected by `communication_protocol`; it always uses plain HTTP on `localhost`.

### Zenoh transport (peer / p2p mode)

When `communication_protocol == "zenoh"`, `_serve_zenoh` opens one `zenoh.Session` in `mode: "peer"` and declares two subscribers: the node's inbox `gradys/msg/<node_id>` and the shared `gradys/msg/broadcast`. There is no FastAPI `/message` server in this mode. The same session is shared with `EmbeddedProvider` for publishing. Discovery follows `auto_scout`:

- **`auto_scout=False`** (default) — multicast is disabled (`scouting/multicast/enabled = false`), gossip is enabled, the session **listens** on `tcp/<own_ip>:<port>` and **connects** to `tcp/<ip:port>` for every other entry in `node_ip_dict`. `node_ip_dict` stays authoritative; works on multicast-blocked LANs.
- **`auto_scout=True`** — default UDP multicast scouting (`224.0.0.224:7446`); peers auto-discover and no explicit endpoints are configured.

**Wire payload is unchanged** — `json.dumps({"message": ..., "source": <id>})` encoded to bytes; the subscriber decodes and forwards `data["message"]`, so `source` is carried-but-discarded exactly like HTTP.

**Threading bridge.** Zenoh subscriber callbacks fire on a Zenoh-owned thread, not the asyncio loop. The callback only does `self._loop.call_soon_threadsafe(self._encapsulator.handle_packet, message)` — handing the message to the runner's single loop. Never call protocol code directly from the callback.

**Until `/protocol/start` has succeeded, `/message` returns 409.** uvicorn binds the port from the moment `start_api()` runs, but the encapsulator that owns `handle_packet` only exists after start. Peers that broadcast during their own `initialize` may see this 409 on receivers that have not started yet.

## Sending — SEND

```python
self.provider.send_communication_command(
    SendMessageCommand(message="hello", destination=3)
)
```

The provider:

1. Looks up `node_ip_dict[3]` → `"192.168.1.12:5000"` (for example), to validate the destination is known.
2. For HTTP transports, fire-and-forgets a POST to `http://192.168.1.12:5000/message` (or `https://` under `"https"`/`"http3"`) with body `{"message": "hello", "source": <my_id>}`, via `_send_to_peer`. For `"zenoh"`, it publishes the same payload to key `gradys/msg/3` (`_zenoh_publish`) on the shared session — the destination's inbox subscription receives it.

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

For HTTP transports the provider iterates `node_ip_dict.items()` and POSTs to every entry whose id is not its own. Same fire-and-forget semantics per peer; a single slow peer does not block the others because each POST is its own asyncio task.

**For HTTP transports, broadcast is O(n) HTTP calls**, not a multicast. Scaling beyond a handful of drones means handling n² traffic on the network. For dense topologies, implement a relay or gossip pattern at the protocol layer rather than broadcasting blindly.

**For `"zenoh"`, broadcast is a single publish** to `gradys/msg/broadcast`; every peer subscribes to that key, so fan-out is handled by Zenoh (and, with `auto_scout=True` on a multicast network, by the network itself) rather than an O(n) loop in the provider.

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

## Peer discovery — none, except zenoh + `auto_scout`

For HTTP transports and for `"zenoh"` with `auto_scout=False`, `node_ip_dict` is static configuration. If a new drone joins the fleet mid-flight, existing drones do not know about it. If a drone's IP changes (DHCP lease expires, switches networks), every other drone's `node_ip_dict` is now wrong and messages to it drop silently.

**Pin IPs.** Use static leases, a dedicated ad-hoc/mesh network, or a private LAN with reserved addresses. Do not rely on DHCP for fleet deployment.

**The one exception** is `communication_protocol="zenoh"` with `auto_scout=True`: Zenoh's multicast scouting discovers peers on the LAN automatically, so `node_ip_dict` is not needed for transport and a drone joining mid-flight can be found. This requires a network that passes UDP multicast (`224.0.0.224:7446`) — many enterprise/cloud/some field Wi-Fi networks block it, in which case use `auto_scout=False` with explicit endpoints.

## Latency and ordering

- **Latency depends on your network.** LAN: sub-millisecond. Wi-Fi mesh at distance: tens to hundreds of ms. 4G/5G backhaul: 50–200 ms typical.
- **Ordering is not guaranteed.** Two SEND commands to the same peer become two independent asyncio tasks; whichever aiohttp finishes the TCP handshake first wins. If order matters, sequence-number your messages at the protocol level.
- **Receiver blocks on `handle_packet`.** The FastAPI route runs `encapsulator.handle_packet(...)` synchronously. A slow protocol handler backs up uvicorn's request queue and delays subsequent incoming messages on that node.

## Security model — there isn't one

`/message` has no authentication and no rate limiting. In the default `"http"` mode there is no TLS either (plain HTTP). The `"https"` and `"http3"` modes encrypt the channel with TLS, but with the default ephemeral cert they do **not** authenticate peers (client verification disabled) — set a shared `certfile`/`keyfile` across the fleet to get peer authentication. The `"zenoh"` transport here is configured without TLS/access-control (Zenoh supports both, but this integration does not enable them), so any Zenoh peer that can reach the network can subscribe to `gradys/msg/**` or publish to it. Either way, anyone reachable on the network can inject messages. Deploy on a private fleet network. (The control plane on `control_api_port` is likewise unauthenticated plain HTTP.)

If you need authentication, wrap the payload in a signed envelope at the protocol level; the transport layer does not help you.

## Debugging communication problems

1. **Is `/message` reachable?** (HTTP transports.) From one drone: `curl -X POST http://<peer>:<port>/message -H 'Content-Type: application/json' -d '{"message":"test","source":0}'` should return `{"status":"ok"}`. Under `"zenoh"` there is no `/message` route — instead check the boot log for `Zenoh peer session open ...` and, for `auto_scout=False`, that every peer's `tcp/<ip:port>` connect endpoint is reachable; `curl` the **control** port (`control_api_port`) for `/protocol/*` instead.
2. **Is `node_ip_dict` right?** A common bug is different drones having drifted copies of `node_ip_dict`. Compare against ground truth on each drone.
3. **Are sends failing silently?** Check each drone's log for `Fire-and-forget task failed:` or `POST ... returned ...` entries. These are the only signal of a send failure.
4. **Is the receiver blocking?** If peer A's `handle_packet` runs slowly, peer B's subsequent sends to A back up. Profile `handle_packet` for hidden blocking calls.

## Related docs

- `→ .claude/docs/runtime-model.md` — when uvicorn (and therefore the `/message` router) starts listening relative to `/protocol/start` and `initialize()`.
- `→ .claude/docs/configuration.md` — `node_ip_dict` shape and shared-origin requirement.
- `→ .claude/docs/protocol-interface.md` — how `handle_packet` is invoked on the asyncio loop.
- `→ /home/fleury/gradys/major_projects/gradys-sim-nextgen/.claude/docs/messages-and-telemetry.md` — the abstract `CommunicationCommand` types (SEND, BROADCAST).

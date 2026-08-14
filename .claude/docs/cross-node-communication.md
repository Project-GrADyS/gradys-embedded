# Cross-Node Communication

How protocols send and receive messages on real hardware. The inter-node **data plane** is selected by the mission's `communication_protocol` (supplied in `POST /mission/load`): for `http`/`https`/`http3` each drone runs a FastAPI server exposing `/message` and peers POST to it; for `zenoh_tcp`/`zenoh_quic` each drone runs an Eclipse Zenoh peer session and messages are pub/sub by key expression. Either way, this data plane is served on the drone's provisioned `data_port` — bound at **mission load**, not at boot — and is **separate from the control plane** (`/mission/*`, `/protocols/*`, `/runs/*`), which runs on `control_api_port` (`→ .claude/docs/runtime-model.md`).

## The `communication` module

Each transport lives behind a `CommunicationBackend` in `gradys_embedded/communication/`. A backend owns **both** sides of one transport — the data-plane *serve* (receive) and the *send*/*broadcast* — so the runner and the provider no longer branch on the protocol string:

| File | Contents |
|---|---|
| `communication/base.py` | `CommunicationBackend` ABC (`serve`, `send`, `broadcast`, `close`, `wait_ready`) + fire-and-forget helper |
| `communication/http.py` | `http`, `https`, `http3` (shared `/message` FastAPI app + peer-`POST` send) |
| `communication/zenoh.py` | `zenoh_tcp`, `zenoh_quic` (shared session/subscribe/publish; differ only in link transport + TLS) |
| `communication/certs.py` | `generate_self_signed_cert()` + `resolve_tls_material()` (TLS material for https/http3/zenoh_quic) |
| `communication/__init__.py` | `create_backend(runner, context)` factory keyed on the mission's `communication_protocol` |

A backend is built **per mission**: `EmbeddedRunner.start_backend` (called from `/mission/load`) builds it via `create_backend(self, context)` from the mission's `MissionContext`, runs `backend.serve()` as a task, and awaits a readiness handshake — load only returns 200 once the listener is actually bound (a bind failure or a 10 s timeout fails the load with 500). Consecutive missions on the same transport reuse the running listener; a different transport releases and rebinds the same `data_port`. `EmbeddedProvider.send_communication_command` calls `backend.send(...)` / `backend.broadcast(...)`. `runner/control_panel.py` holds **only** the control plane (`/protocols`, `/mission`, `/runs`).

Sources: `gradys_embedded/communication/` (transports), `gradys_embedded/runner/runner.py` (boot/dispatch), `gradys_embedded/encapsulator/embedded.py` (`send_communication_command`, sender).

## The `/message` endpoint — what HTTP-transport nodes expose

Defined in the message app built by `communication.http.build_message_app(runner)` (http/https/http3 only):

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

The server listens on `0.0.0.0:<data_port>` — the provisioned bind port; `node_ip_dict` no longer supplies it, so every peer's entry for this node must point at its `data_port`. The control plane (`/mission/*`, `/protocols/*`, `/runs/*`) is a **separate** FastAPI app on `control_api_port` (`→ .claude/docs/runtime-model.md`). Both share the asyncio loop with the rest of the runner — there is no separate thread.

## Transport — http (default) vs https vs http3 vs zenoh_tcp vs zenoh_quic

The data plane runs over one of five transports, selected by the mission's `communication_protocol` in `POST /mission/load` (default `"http"`); only the chosen one is ever served. For the three HTTP transports the FastAPI app, the `/message` route, and the wire payload are **identical**; only the server and client transport change. The two zenoh transports are likewise identical except for their link transport (TCP vs QUIC/TLS). `communication_protocol` must be the same on every node.

| | `"http"` (default) | `"https"` | `"http3"` | `"zenoh_tcp"` | `"zenoh_quic"` |
|---|---|---|---|---|---|
| Model | request/response, IP-addressed | request/response, IP-addressed | request/response, IP-addressed | **pub/sub, key-addressed, peer (p2p)** | **pub/sub, key-addressed, peer (p2p)** |
| Server | uvicorn, HTTP/1.1 over TCP | uvicorn, HTTP/1.1 over TLS (TCP) | Hypercorn `quic_bind`, HTTP/3 over QUIC (UDP), TLS 1.3 | Zenoh peer session, **TCP** links | Zenoh peer session, **QUIC** links (TLS 1.3) |
| Addressing | `http://<addr>/message` | `https://<addr>/message` | `https://<addr>/message` | keys `gradys/msg/<dest_id>`, `gradys/msg/broadcast` | keys `gradys/msg/<dest_id>`, `gradys/msg/broadcast` |
| Client | shared `aiohttp.ClientSession` | shared `aiohttp.ClientSession` (per-request `ssl=`) | lazily-created `niquests.AsyncSession` | shared Zenoh session (`.put`) | shared Zenoh session (`.put`) |
| Endpoints | `node_ip_dict` | `node_ip_dict` | `node_ip_dict` | `tcp/<ip:port>` from `node_ip_dict` (`auto_scout=False`) **or** multicast scouting (`auto_scout=True`) | `quic/<ip:port>` from `node_ip_dict` (`auto_scout=False`) **or** multicast scouting (`auto_scout=True`) |
| TLS | none | server cert | server cert | **none** | **mandatory**; fleet-wide shared cert |
| Deps | core install | core install | `pip install "gradys-embedded[http3]"` | `pip install "gradys-embedded[zenoh]"` | `pip install "gradys-embedded[zenoh]"` |

`"https"` and `"http3"` both require the server to present a certificate. If `certfile`/`keyfile` are provisioned, the server binds with them and the client **verifies peers against `certfile`**. If they are omitted, the server binds with an **ephemeral self-signed certificate generated when the backend is built at mission load** (temp file, not persisted) and the client disables verification (`ssl=False` for https, `verify=False` for http3) — the channel is still encrypted, but peers are not authenticated. See `→ .claude/docs/configuration.md`.

The local `uav_api` connection is unaffected by `communication_protocol`; it always uses plain HTTP on `localhost`.

### Zenoh transports (peer / p2p mode) — `zenoh_tcp` and `zenoh_quic`

For both zenoh transports, `ZenohBackend.serve` opens one `zenoh.Session` in `mode: "peer"` and declares two subscribers: the node's inbox `gradys/msg/<node_id>` and the shared `gradys/msg/broadcast`. There is no FastAPI `/message` server in these modes. The same session is used by the backend's `send`/`broadcast` for publishing. The **only** difference between the two is the link scheme:

- **`zenoh_tcp`** — links are `tcp/<ip:port>`, no TLS.
- **`zenoh_quic`** — links are `quic/<ip:port>` over TLS 1.3. Zenoh QUIC **mandates** TLS and verifies the listener's cert against a `root_ca_certificate`; the only relaxation is `verify_name_on_connect=false` (ignores hostname/SAN, since endpoints are IP-addressed). The backend sets `listen_certificate`/`listen_private_key`/`root_ca_certificate` all to the configured cert (a self-signed cert is its own CA). **Every node must therefore share the SAME certificate** — see the security model below. A mission selecting `zenoh_quic` on a node with no provisioned `certfile` is **rejected with 400 at `/mission/load`** (the ephemeral fallback would make the fleet silently fail to mesh); the loud warning in `serve` remains only as a backstop for backends built outside the mission path.

Discovery follows `auto_scout` (same for both):

- **`auto_scout=False`** (default) — multicast disabled (`scouting/multicast/enabled = false`), gossip enabled, the session **listens** on `<scheme>/<own_ip>:<data_port>` (the IP comes from this node's own `node_ip_dict` entry — which is why load returns 400 if the mission's map omits it; the port is always the provisioned `data_port`) and **connects** to `<scheme>/<ip:port>` for every other entry in `node_ip_dict`. The map stays authoritative; works on multicast-blocked LANs.
- **`auto_scout=True`** — default UDP multicast scouting (`224.0.0.224:7446`); peers auto-discover, and this is the one configuration where `/mission/load` accepts a missing `node_ip_dict`. For `zenoh_quic` the backend additionally pins an explicit `quic/0.0.0.0:<data_port>` listen endpoint so advertised data links are QUIC rather than the default TCP listener.

**Wire payload is unchanged** — `json.dumps({"message": ..., "source": <id>})` encoded to bytes; the subscriber decodes and forwards `data["message"]`, so `source` is carried-but-discarded exactly like HTTP.

**Threading bridge.** Zenoh subscriber callbacks fire on a Zenoh-owned thread, not the asyncio loop. The callback only does `runner._loop.call_soon_threadsafe(runner._encapsulator.handle_packet, message)` — handing the message to the runner's single loop. Never call protocol code directly from the callback.

**Until `/mission/start` has succeeded, inbound messages are dropped.** The data plane (uvicorn for HTTP, the Zenoh session for zenoh) binds at `/mission/load` — before a load, nothing listens on `data_port` at all and a peer's send fails at the connection level. Between load and start the listener is up but the encapsulator that owns `handle_packet` does not exist yet — so HTTP `/message` returns 409 and the Zenoh subscriber callback drops the sample. (The same guard returns after `/mission/stop` or `/mission/reset` tears the protocol down.) Peers that broadcast during their own `initialize` may be ignored by receivers that have not started yet.

## Sending — SEND

```python
self.provider.send_communication_command(
    SendMessageCommand(message="hello", destination=3)
)
```

The provider builds `{"message": "hello", "source": <my_id>}` and calls `backend.send(3, payload)`. The backend then:

- **HTTP transports** — look up `node_ip_dict[3]` → `"192.168.1.12:5000"`, then fire-and-forget a POST to `http://192.168.1.12:5000/message` (or `https://` under `"https"`/`"http3"`).
- **zenoh transports** — publish the payload to key `gradys/msg/3` on the shared session; the destination's inbox subscription receives it. No `node_ip_dict` lookup is needed (the key carries the destination).

Failure modes:

- **Unknown destination** (HTTP transports) — `node_ip_dict.get(destination)` returns `None`; the backend logs `"Unknown destination node <id>"` and drops the message. No exception reaches the protocol.
- **No peer map at all** (HTTP transports) — load-time validation makes this unreachable in the normal path, but the backend guards anyway: it logs `"No peer map for this mission"` and drops the send/broadcast rather than crash a flying protocol.
- **Peer unreachable / timeout / connection refused** — the aiohttp task fails; `_log_task_exception` logs the error. The protocol is never notified.
- **Peer returns non-200** — the backend logs `"POST <url> returned <status>: <body>"`. The protocol is never notified.

**The protocol never learns about delivery outcomes.** Design all peer interactions as unreliable. If you need acknowledgments, implement them at the protocol level (e.g., expect an ACK message within a timer window; resend on timeout).

## Sending — BROADCAST

```python
self.provider.send_communication_command(
    BroadcastMessageCommand(message="heartbeat")
)
```

For HTTP transports the backend iterates `node_ip_dict.items()` and POSTs to every entry whose id is not its own. Same fire-and-forget semantics per peer; a single slow peer does not block the others because each POST is its own asyncio task.

**For HTTP transports, broadcast is O(n) HTTP calls**, not a multicast. Scaling beyond a handful of drones means handling n² traffic on the network. For dense topologies, implement a relay or gossip pattern at the protocol layer rather than broadcasting blindly.

**For the zenoh transports, broadcast is a single publish** to `gradys/msg/broadcast`; every peer subscribes to that key, so fan-out is handled by Zenoh (and, with `auto_scout=True` on a multicast network, by the network itself) rather than an O(n) loop in the backend.

## Fire-and-forget — what it really means

HTTP-transport sends go through the backend's `_fire_and_forget` (`communication/base.py`; the provider has an identical one for mobility commands):

```python
def _fire_and_forget(self, coro) -> None:
    task = self._runner._loop.create_task(coro)
    self._pending_tasks.add(task)                       # asyncio holds only a weak ref;
    task.add_done_callback(self._pending_tasks.discard) # keep the send alive until done
    task.add_done_callback(self._log_task_exception)
```

- The call to `send_communication_command` returns immediately. Your protocol does not wait for the HTTP request to be sent, let alone acknowledged.
- A single asyncio task per send; aiohttp reuses the shared `ClientSession` connection pool.
- Exceptions inside the task are logged via the done callback but never propagate.
- There is **no retry**. A one-shot failure is a lost message.

## Peer discovery — none, except zenoh + `auto_scout`

For HTTP transports and for the zenoh transports with `auto_scout=False`, `node_ip_dict` is mission-supplied — every `POST /mission/load` carries the map (400 without it), and it is fixed into the mission's context for the mission's duration. **Within a running mission there is no discovery**: a new drone joining mid-flight is unknown to the others, and if a drone's IP changes (DHCP lease expires, switches networks) every other drone's map is now wrong and messages to it drop silently. **Between missions**, though, the fleet can gain or lose a drone, or change addresses, simply by loading the next mission with an updated map — no drone needs re-provisioning, since the map is no longer part of the provisioned configuration.

**Pin IPs.** Use static leases, a dedicated ad-hoc/mesh network, or a private LAN with reserved addresses. Do not rely on DHCP for fleet deployment.

**The one exception** is `communication_protocol="zenoh_tcp"` or `"zenoh_quic"` with `auto_scout=True`: Zenoh's multicast scouting discovers peers on the LAN automatically, so `node_ip_dict` is not needed for transport and a drone joining mid-flight can be found. This requires a network that passes UDP multicast (`224.0.0.224:7446`) — many enterprise/cloud/some field Wi-Fi networks block it, in which case use `auto_scout=False` with explicit endpoints.

## Latency and ordering

- **Latency depends on your network.** LAN: sub-millisecond. Wi-Fi mesh at distance: tens to hundreds of ms. 4G/5G backhaul: 50–200 ms typical.
- **Ordering is not guaranteed.** Two SEND commands to the same peer become two independent asyncio tasks; whichever aiohttp finishes the TCP handshake first wins. If order matters, sequence-number your messages at the protocol level.
- **Receiver blocks on `handle_packet`.** The FastAPI route runs `encapsulator.handle_packet(...)` synchronously. A slow protocol handler backs up uvicorn's request queue and delays subsequent incoming messages on that node.

## Security model — there isn't one

`/message` has no authentication and no rate limiting. In the default `"http"` mode there is no TLS either (plain HTTP). The `"https"` and `"http3"` modes encrypt the channel with TLS, but with the default ephemeral cert they do **not** authenticate peers (client verification disabled) — set a shared `certfile`/`keyfile` across the fleet to get peer authentication. `"zenoh_tcp"` runs without TLS, so any Zenoh peer that can reach the network can subscribe to `gradys/msg/**` or publish to it. `"zenoh_quic"` is the **only transport that authenticates peers by default**: QUIC mandates TLS and verifies every peer's cert against the shared `root_ca_certificate`, so only nodes holding the fleet-wide cert can join (name verification is disabled, but the cert/CA check is not). Note that with an *ephemeral* per-node cert (no `certfile` configured) `zenoh_quic` peers reject each other — the channel is encrypted but the fleet cannot actually communicate; configure a shared cert. Otherwise, anyone reachable on the network can inject messages — deploy on a private fleet network. (The control plane on `control_api_port` is likewise unauthenticated plain HTTP.)

If you need authentication, wrap the payload in a signed envelope at the protocol level; the transport layer does not help you.

## Debugging communication problems

1. **Is `/message` reachable?** (HTTP transports.) It only exists once a mission has loaded — before `/mission/load` nothing listens on `data_port` and the connection is refused. From one drone: `curl -X POST http://<peer>:<data_port>/message -H 'Content-Type: application/json' -d '{"message":"test","source":0}'` should return `{"status":"ok"}` (or 409 before `/mission/start` — which still proves the listener is up). Under the zenoh transports there is no `/message` route — instead check the mission-load log for `Zenoh peer session open (transport=tcp|quic, ...)` and, for `auto_scout=False`, that every peer's `<scheme>/<ip:port>` connect endpoint is reachable; `curl` the **control** port (`control_api_port`, `GET /mission/status`) instead. For `zenoh_quic`, remember `/mission/load` already rejects it with 400 when no shared `certfile` is provisioned.
2. **Is `node_ip_dict` right?** A common bug is drones loaded with drifted copies of the map, or entries not pointing at each peer's `data_port`. `GET /mission/status` echoes the map each drone actually loaded — diff them across the fleet.
3. **Are sends failing silently?** Check each drone's log for `Fire-and-forget task failed:` or `POST ... returned ...` entries. These are the only signal of a send failure.
4. **Is the receiver blocking?** If peer A's `handle_packet` runs slowly, peer B's subsequent sends to A back up. Profile `handle_packet` for hidden blocking calls.

## Related docs

- `→ .claude/docs/runtime-model.md` — when the data plane binds (`/mission/load`) relative to `/mission/start` and `initialize()`.
- `→ .claude/docs/configuration.md` — `node_ip_dict` shape, `data_port`, and the shared-frame requirement.
- `→ .claude/docs/protocol-interface.md` — how `handle_packet` is invoked on the asyncio loop.
- `→ https://github.com/Project-GrADyS/gradys-sim-nextgen/blob/main/.claude/docs/messages-and-telemetry.md` — the abstract `CommunicationCommand` types (SEND, BROADCAST).

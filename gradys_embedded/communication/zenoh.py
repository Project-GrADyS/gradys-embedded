"""Zenoh peer/p2p transports: ``zenoh_tcp`` and ``zenoh_quic``.

Both run Eclipse Zenoh in peer mode and exchange messages by key expression
(``gradys/msg/<node_id>`` for unicast, ``gradys/msg/broadcast`` for broadcast). They share the
entire session/subscribe/publish path and differ only in the link transport:
  - ``zenoh_tcp``  — ``tcp/<ip:port>`` links, no TLS.
  - ``zenoh_quic`` — ``quic/<ip:port>`` links over TLS 1.3.

Zenoh QUIC mandates TLS and verifies the listener's cert against a ``root_ca_certificate``; the
only relaxation is ``verify_name_on_connect=false`` (ignores hostname/SAN). So for multi-node
``zenoh_quic`` every node must share the SAME certificate (used as both listen identity and trust
anchor). ``serve`` warns loudly when no shared cert is configured.

Requires the ``gradys-embedded[zenoh]`` extra.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from gradys_embedded.communication import certs
from gradys_embedded.communication.base import CommunicationBackend

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner

_QUIC_PROTOCOL = "zenoh_quic"


class ZenohBackend(CommunicationBackend):
    def __init__(self, runner: "EmbeddedRunner", configuration) -> None:
        super().__init__(runner, configuration)
        self._is_quic = self._configuration.communication_protocol == _QUIC_PROTOCOL
        self._scheme = "quic" if self._is_quic else "tcp"
        self._session = None
        self._subscribers: list = []
        # Releases serve(). The transport is a mission parameter, so this session
        # lasts for a mission rather than the process.
        self._stop = asyncio.Event()

    def _build_config(self):
        """Build a Zenoh peer-mode Config. Option A (auto_scout) uses multicast scouting; Option B
        builds explicit listen/connect endpoints from node_ip_dict. QUIC adds TLS material."""
        import zenoh

        cfg = zenoh.Config()
        cfg.insert_json5("mode", json.dumps("peer"))

        if self._is_quic:
            certfile, keyfile = certs.resolve_tls_material(self._configuration, self._logger)
            cfg.insert_json5("transport/link/tls/listen_certificate", json.dumps(certfile))
            cfg.insert_json5("transport/link/tls/listen_private_key", json.dumps(keyfile))
            # Self-signed cert is its own CA; endpoints are IP-addressed so skip SAN matching.
            cfg.insert_json5("transport/link/tls/root_ca_certificate", json.dumps(certfile))
            cfg.insert_json5("transport/link/tls/verify_name_on_connect", "false")

        if self._configuration.auto_scout:
            # Option A: rely on Zenoh's default UDP multicast scouting for discovery. For QUIC,
            # pin an explicit listen endpoint so advertised data links are QUIC rather than the
            # default TCP listener.
            if self._is_quic:
                cfg.insert_json5("listen/endpoints", json.dumps([f"quic/0.0.0.0:{self._port}"]))
            return cfg

        # Option B: no multicast; connect explicitly to peers listed in node_ip_dict.
        peers = self._configuration.node_ip_dict
        if not peers or self._configuration.node_id not in peers:
            # Backstop behind the load-time 400: reached only if a backend is
            # built outside the mission path. Raising here fails the bind
            # handshake instead of leaving a dead serve task.
            raise RuntimeError(
                "zenoh without auto_scout requires a node_ip_dict that includes "
                "this node's own entry to build its listen endpoint."
            )
        cfg.insert_json5("scouting/multicast/enabled", "false")
        cfg.insert_json5("scouting/gossip/enabled", "true")
        own_ip = peers[self._configuration.node_id].rsplit(":", 1)[0]
        cfg.insert_json5("listen/endpoints", json.dumps([f"{self._scheme}/{own_ip}:{self._port}"]))
        connect = [
            f"{self._scheme}/{addr}"
            for nid, addr in self._configuration.node_ip_dict.items()
            if nid != self._configuration.node_id
        ]
        cfg.insert_json5("connect/endpoints", json.dumps(connect))
        return cfg

    async def serve(self) -> None:
        import zenoh

        if self._is_quic and self._configuration.certfile is None:
            self._logger.warning(
                "zenoh_quic is using an EPHEMERAL self-signed certificate; QUIC verifies peer "
                "certs against a shared root CA, so peers on other nodes will NOT trust each "
                "other. Set a fleet-wide certfile/keyfile (identical on every node) for "
                "multi-node QUIC. (gradys-sitl-tester auto-generates one shared pair.)"
            )

        self._session = zenoh.open(self._build_config())

        def on_sample(sample) -> None:
            # Runs on a Zenoh background thread — marshal onto the runner's event loop.
            if self._runner._encapsulator is None:
                return  # protocol not started yet; drop, mirroring /message 409-before-start
            try:
                data = json.loads(bytes(sample.payload))
                message = data["message"]
            except Exception as e:
                self._logger.error(f"Failed to decode zenoh message: {e}")
                return
            self._runner._loop.call_soon_threadsafe(self._runner._encapsulator.handle_packet, message)

        node_id = self._configuration.node_id
        self._subscribers = [
            self._session.declare_subscriber(f"gradys/msg/{node_id}", on_sample),
            self._session.declare_subscriber("gradys/msg/broadcast", on_sample),
        ]
        cert_note = (
            f", shared_cert={self._configuration.certfile is not None}" if self._is_quic else ""
        )
        self._logger.info(
            f"Zenoh peer session open (transport={self._scheme}, "
            f"auto_scout={self._configuration.auto_scout}{cert_note}); "
            f"subscribed to gradys/msg/{node_id} and gradys/msg/broadcast"
        )
        # zenoh.open and the subscriber declarations above are synchronous and
        # raise directly, so reaching this point means the session is live.
        self._signal_ready()
        # Keep the data plane alive until close(). Rebuilding the session per
        # mission is also what lets a mission supply a different peer map --
        # zenoh fixes its connect endpoints when the session opens, so a
        # long-lived session could never pick one up.
        await self._stop.wait()

    def send(self, dest_node_id: int, payload: dict) -> None:
        self._publish(f"gradys/msg/{dest_node_id}", payload)

    def broadcast(self, payload: dict) -> None:
        # One publish on the shared broadcast key — not an O(n) per-peer loop.
        self._publish("gradys/msg/broadcast", payload)

    def _publish(self, key: str, payload: dict) -> None:
        # Zenoh put is a fast local enqueue; failures are logged, never raised (fire-and-forget).
        try:
            self._session.put(key, json.dumps(payload).encode())
        except Exception as e:
            self._logger.error(f"Zenoh put to {key!r} failed: {e}")

    async def close(self) -> None:
        self._stop.set()
        for subscriber in self._subscribers:
            try:
                subscriber.undeclare()
            except Exception as e:
                # Dropped with the session anyway; never let teardown of one
                # transport block the next one from binding.
                self._logger.debug(f"Subscriber undeclare failed: {e}")
        self._subscribers = []
        if self._session is not None:
            self._session.close()
            self._session = None

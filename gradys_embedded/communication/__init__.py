"""Inter-node message transports.

Each ``communication_protocol`` value maps to one :class:`CommunicationBackend` that owns both the
data-plane serve (receive) side and the send/broadcast side. Use :func:`create_backend` to build the
backend for a runner's configured protocol.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gradys_embedded.communication.base import CommunicationBackend

if TYPE_CHECKING:
    from gradys_embedded.runner.runner import EmbeddedRunner

__all__ = ["CommunicationBackend", "create_backend"]


def create_backend(runner: "EmbeddedRunner") -> CommunicationBackend:
    protocol = runner._configuration.communication_protocol
    if protocol in ("http", "https", "http3"):
        from gradys_embedded.communication.http import HttpBackend

        return HttpBackend(runner)
    if protocol in ("zenoh_tcp", "zenoh_quic"):
        from gradys_embedded.communication.zenoh import ZenohBackend

        return ZenohBackend(runner)
    raise ValueError(f"Invalid communication_protocol {protocol!r}")

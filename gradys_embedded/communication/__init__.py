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


HTTP_PROTOCOLS = ("http", "https", "http3")
ZENOH_PROTOCOLS = ("zenoh_tcp", "zenoh_quic")
PROTOCOLS = HTTP_PROTOCOLS + ZENOH_PROTOCOLS

# Which optional extra each protocol needs, for a clear error at mission load
# rather than an ImportError inside a serve task nobody is awaiting.
PROTOCOL_EXTRAS = {
    "http3": ("hypercorn", 'gradys-embedded[http3]'),
    "zenoh_tcp": ("zenoh", 'gradys-embedded[zenoh]'),
    "zenoh_quic": ("zenoh", 'gradys-embedded[zenoh]'),
}


def missing_extra(protocol: str) -> str | None:
    """Return the pip extra a protocol needs but does not have installed.

    Checked without importing the module, so the lazy-import design is preserved:
    nothing heavyweight is loaded unless the protocol is actually selected.
    """
    requirement = PROTOCOL_EXTRAS.get(protocol)
    if requirement is None:
        return None

    import importlib.util

    module, extra = requirement
    return None if importlib.util.find_spec(module) is not None else extra


def create_backend(runner: "EmbeddedRunner", configuration=None) -> CommunicationBackend:
    """Build the backend for a configuration's transport.

    `configuration` is the MISSION's, not the provisioned one -- the transport,
    the peer map and the TLS material are all mission-scoped now.
    """
    configuration = configuration if configuration is not None else runner._configuration
    protocol = configuration.communication_protocol
    if protocol in HTTP_PROTOCOLS:
        from gradys_embedded.communication.http import HttpBackend

        return HttpBackend(runner, configuration)
    if protocol in ZENOH_PROTOCOLS:
        from gradys_embedded.communication.zenoh import ZenohBackend

        return ZenohBackend(runner, configuration)
    raise ValueError(f"Invalid communication_protocol {protocol!r}")

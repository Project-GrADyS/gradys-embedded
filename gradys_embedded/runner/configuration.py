from dataclasses import dataclass


@dataclass
class RunnerConfiguration:
    node_id: int

    node_ip_dict: dict[int, str]
    """Maps node_id to 'ip:port' string for the message API (e.g. '192.168.1.10:8000')"""

    initial_position: tuple[float, float, float]
    """Initial position for the UAV in Cartesian Coordinates (x, y, z)."""

    uav_api_port: int
    """Port for the local UAV HTTP API"""

    control_api_port: int
    """Dedicated HTTP port for the control plane (`/protocol/setup`, `/protocol/start`).

    Served over plain HTTP regardless of `communication_protocol`. The data-plane message
    API (`/message`) is served separately on this node's `node_ip_dict` port, so the two
    never share a port. Must be unique per node when several nodes run on the same host."""

    origin_gps_coordinates: tuple[float, float, float] | None = None
    """Origin GPS coordinates (latitude, longitude, altitude) for converting between GPS and Cartesian coordinates. If None, drone current position will be used as origin."""

    x_axis_degrees: float | None = None
    """Clockwise rotation of the protocol's x-axis from true north, in degrees. The y-axis is always 90 degrees clockwise from the x-axis; 0.0 keeps the NEU convention (x=North, y=East). If None, the runner initializes it from the drone's current heading at boot (same pattern as origin_gps_coordinates). Must be identical on every node in the fleet — mismatched rotations silently desynchronize cartesian frames."""

    telemetry_interval: float = 0.5
    """Seconds between telemetry polls"""

    communication_protocol: str = "http"
    """Transport for the inter-node message API. One of:
    - "http" (default): HTTP/1.1 over TCP via uvicorn, plain (no TLS).
    - "https": HTTP/1.1 over TLS via uvicorn, using certfile/keyfile (or an ephemeral self-signed cert).
    - "http3": HTTP/3 over QUIC (UDP) via Hypercorn, TLS 1.3; requires `pip install "gradys-embedded[http3]"`.
    - "zenoh_tcp": Eclipse Zenoh pub/sub in peer (p2p) mode over TCP links (no TLS); requires `pip install "gradys-embedded[zenoh]"`. See `auto_scout`.
    - "zenoh_quic": Eclipse Zenoh pub/sub in peer (p2p) mode over QUIC links (TLS 1.3); requires `pip install "gradys-embedded[zenoh]"`. Needs a fleet-wide shared certfile/keyfile (identical on every node) for multi-node operation — see certfile. See `auto_scout`.
    Must be identical on every node in the fleet — mixed transports cannot interoperate. Unrelated to uav_api's own --udp flag; the local uav_api connection always uses plain HTTP on localhost."""

    auto_scout: bool = False
    """Whether nodes discover each other automatically instead of using the static `node_ip_dict`.

    Only implemented for the zenoh transports (`zenoh_tcp`, `zenoh_quic`):
    - False (default): Zenoh peer mode with multicast disabled and explicit `connect.endpoints`
      built from `node_ip_dict` (works on multicast-blocked LANs; keeps `node_ip_dict` authoritative).
    - True: Zenoh peer mode using default UDP multicast scouting (224.0.0.224:7446); peers
      auto-discover and `node_ip_dict` is not needed for transport.
    Has NO effect for "http"/"https"/"http3" (those transports have no discovery); it is accepted
    but ignored for them."""

    certfile: str | None = None
    """TLS certificate (PEM). Used by "https"/"http3" (message-API server identity) and by
    "zenoh_quic" (QUIC TLS link identity AND fleet trust anchor — name verification is disabled so
    IP-addressed endpoints work). For "https"/"http3" it is also the client trust anchor to verify
    peers. If None, an ephemeral self-signed certificate is generated at boot: harmless for
    "https"/"http3" (client verification is then disabled), but for "zenoh_quic" peers on other
    nodes will NOT trust each other — so a fleet-wide shared certfile/keyfile is effectively required
    for multi-node "zenoh_quic" (gradys-sitl-tester auto-generates one shared pair). Ignored when
    communication_protocol is "http" or "zenoh_tcp"."""

    keyfile: str | None = None
    """TLS private key (PEM) paired with certfile. Required when certfile is provided. Ignored when communication_protocol is "http"/"zenoh_tcp" or when certfile is None."""

    def __post_init__(self) -> None:
        valid = {"http", "https", "http3", "zenoh_tcp", "zenoh_quic"}
        if self.communication_protocol not in valid:
            raise ValueError(
                f"Invalid communication_protocol {self.communication_protocol!r}; must be one of {sorted(valid)}"
            )
        if self.auto_scout and self.communication_protocol not in ("zenoh_tcp", "zenoh_quic"):
            import logging

            logging.getLogger(__name__).warning(
                "auto_scout=True has no effect for communication_protocol=%r; it is only "
                "implemented for the zenoh transports. Ignoring.",
                self.communication_protocol,
            )

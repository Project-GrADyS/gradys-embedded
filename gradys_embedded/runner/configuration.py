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
    Must be identical on every node in the fleet — mixed transports cannot interoperate. Unrelated to uav_api's own --udp flag; the local uav_api connection always uses plain HTTP on localhost."""

    certfile: str | None = None
    """TLS certificate (PEM) for the message-API server in "https" and "http3" modes. When set, it is also used as the client trust anchor to verify peers. If None, the server binds with an ephemeral self-signed certificate generated at boot and client-side peer verification is disabled. Ignored when communication_protocol is "http"."""

    keyfile: str | None = None
    """TLS private key (PEM) paired with certfile. Required when certfile is provided. Ignored when communication_protocol is "http" or when certfile is None."""

    def __post_init__(self) -> None:
        valid = {"http", "https", "http3"}
        if self.communication_protocol not in valid:
            raise ValueError(
                f"Invalid communication_protocol {self.communication_protocol!r}; must be one of {sorted(valid)}"
            )

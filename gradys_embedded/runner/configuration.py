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

    udp: bool = False
    """Serve the inter-node message API over QUIC/HTTP3 (UDP) via Hypercorn instead of HTTP/TCP via uvicorn. TCP by default. Must be identical on every node in the fleet. Does not affect the local uav_api connection, which always uses plain HTTP on localhost."""

    certfile: str | None = None
    """TLS certificate (PEM) for the QUIC server in udp mode. When set, it is also used as the client trust anchor to verify peers. If None, the server binds with an ephemeral self-signed certificate generated at boot and client-side peer verification is disabled. Ignored when udp is False."""

    keyfile: str | None = None
    """TLS private key (PEM) paired with certfile. Required when certfile is provided. Ignored when udp is False or when certfile is None."""

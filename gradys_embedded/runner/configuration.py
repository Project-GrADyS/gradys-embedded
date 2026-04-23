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

    telemetry_interval: float = 0.5
    """Seconds between telemetry polls"""

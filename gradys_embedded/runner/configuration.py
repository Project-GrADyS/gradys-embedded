from dataclasses import dataclass


@dataclass
class RunnerConfiguration:
    node_id: int

    node_ip_dict: dict[int, str]
    """Maps node_id to 'ip:port' string for the message API (e.g. '192.168.1.10:8000')"""

    origin_gps_coordinates: tuple[float, float, float]

    uav_api_port: int
    """Port for the local UAV HTTP API"""

    telemetry_interval: float = 0.5
    """Seconds between telemetry polls"""


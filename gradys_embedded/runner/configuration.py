from dataclasses import dataclass


@dataclass
class RunnerConfiguration:
    # --- Provisioned: bound to the machine and its network --------------------
    # Set once by gradys-fleet for the (image, network) pair. Stable for as long
    # as a drone sits on a given LAN.

    node_id: int

    uav_api_port: int
    """Port for the local UAV HTTP API"""

    control_api_port: int
    """Dedicated HTTP port for the control plane (`/mission`, `/protocols`, `/runs`).

    Served over plain HTTP regardless of `communication_protocol`. The data-plane message
    API (`/message`) is served separately on `data_port`, so the two never share a port.
    Must be unique per node when several nodes run on the same host."""

    data_port: int | None = None
    """Port this node binds for the inter-node data plane.

    When None it is derived from `node_ip_dict[node_id]`, which is how configs
    expressed it before the peer map became mission-supplied."""

    # --- Mission-supplied: sent per run by the mission layer -------------------
    # These describe an experiment, not a machine, so gradys-fleet does not render
    # them. `POST /mission/load` carries them, along with communication_protocol
    # and auto_scout further down. Every one except initial_position must be
    # IDENTICAL across the fleet, and nothing here can validate that -- which is
    # why `/mission/status` echoes them for the caller to check before starting.

    node_ip_dict: dict[int, str] | None = None
    """Maps node_id to 'ip:port' string for the message API (e.g. '192.168.1.10:8000').

    Bare `host:port`, no scheme. Supplying it per mission is what lets the fleet
    gain or lose a drone without re-provisioning every other one. Every transport
    honours a change, including zenoh: its session is opened per mission, so its
    connect endpoints are built from whatever map that mission supplied."""

    initial_position: tuple[float, float, float] | None = None
    """Initial position for the UAV in Cartesian Coordinates (x, y, z).

    The z component doubles as the takeoff altitude. Must be set before
    `/mission/setup` can fly the drone anywhere."""

    origin_gps_coordinates: tuple[float, float, float] | None = None
    """Origin GPS coordinates (latitude, longitude, altitude) for converting between GPS and Cartesian coordinates. If None, the drone's own position at mission load is used as the origin — which puts every drone in a different frame unless the whole fleet is given the same explicit value."""

    x_axis_degrees: float | None = None
    """Clockwise rotation of the protocol's x-axis from true north, in degrees. The y-axis is always 90 degrees clockwise from the x-axis; 0.0 keeps the NEU convention (x=North, y=East). If None, it is initialized from the drone's own heading at mission load. Must be identical on every node in the fleet — mismatched rotations silently desynchronize cartesian frames."""

    telemetry_interval: float = 0.5
    """Seconds between telemetry polls"""

    communication_protocol: str = "http"
    """Transport for the inter-node message API.

    MISSION-SUPPLIED. The value here is only the default; `POST /mission/load`
    chooses the transport per run, and only the chosen one is ever served — the
    data plane binds nothing until a mission loads. A mission on a different
    transport rebinds the same `data_port`; consecutive missions on the same one
    reuse the listener.

    One of:
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
    peers. Provisioned, not mission-supplied — it is a file on this machine.

    If None, an ephemeral self-signed certificate is generated when the backend is built at mission
    load: harmless for "https"/"http3" (client verification is then disabled), but "zenoh_quic"
    peers would NOT trust each other, so `/mission/load` rejects that transport outright rather than
    letting a fleet fail to mesh silently. A fleet-wide shared certfile/keyfile (identical on every
    node) is what makes zenoh_quic selectable; gradys-fleet provisions one, and gradys-sitl-tester
    auto-generates one. Ignored when communication_protocol is "http" or "zenoh_tcp"."""

    keyfile: str | None = None
    """TLS private key (PEM) paired with certfile. Required when certfile is provided. Ignored when communication_protocol is "http"/"zenoh_tcp" or when certfile is None."""

    runs_dir: str = "~/gradys_runs"
    """Directory holding one subdirectory per mission run.

    Each run gets `{runs_dir}/run_<timestamp>[_label]/` containing the statistics
    CSVs, the resource-usage CSV, a per-run log, and RUN_INFO.json. This is what
    `GET /runs` lists and what the download endpoints serve, so it must be
    writable by the service user and should live on persistent storage."""

    protocols_dir: str = "~/gradys_protocols"
    """Directory that uploaded protocol modules are written to and imported from.

    `POST /protocols/upload` writes here, and it is placed on `sys.path` so
    protocols can be loaded by module name. Pre-deployed protocols shipped by
    provisioning can live here too."""

    min_free_disk_mb: int = 200
    """Refuse to start a new run when the runs_dir filesystem has less free space
    than this, in megabytes.

    Nothing is ever auto-deleted -- losing flight data to an automatic prune is
    worse than a refused start -- so this is the backstop that turns "the SD card
    filled up mid-flight" into a clear error before takeoff. Set 0 to disable."""

    telemetry_landed_alt: float = 0.5
    """Relative altitude, in metres, below which the vehicle counts as landed.

    uav_api exposes no flight-mode or armed-state endpoint, so after a stop
    issues RTL the service infers touchdown from the telemetry it already polls."""

    def resolve_data_port(self) -> int:
        """The port this node binds for the data plane.

        Prefers the provisioned `data_port`; falls back to the peer map so
        configs written before the split keep working.
        """
        if self.data_port is not None:
            return self.data_port

        if self.node_ip_dict and self.node_id in self.node_ip_dict:
            return int(self.node_ip_dict[self.node_id].rsplit(":", 1)[1])

        raise ValueError(
            f"Cannot determine the data-plane port for node {self.node_id}: set "
            f"`data_port`, or include this node in `node_ip_dict`."
        )

    def __post_init__(self) -> None:
        # Expanded here rather than at each use site, so a config written as
        # "~/gradys_runs" behaves the same whether it came from TOML or was
        # constructed in Python.
        import os

        self.runs_dir = os.path.expanduser(self.runs_dir)
        self.protocols_dir = os.path.expanduser(self.protocols_dir)

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

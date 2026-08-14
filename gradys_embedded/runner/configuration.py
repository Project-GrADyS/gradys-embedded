"""Provisioned vs mission-scoped configuration.

Two dataclasses with a hard boundary between them:

- :class:`RunnerConfiguration` is what a machine needs to START the service.
  It is bound to the drone and its network, rendered once by gradys-fleet, and
  is the ONLY thing the TOML loader will accept.
- :class:`MissionConfiguration` describes an EXPERIMENT. It exists solely so
  ``POST /mission/load`` has a validated shape to carry; it cannot be
  provisioned, and the mission API is the only gateway for it.

:class:`MissionContext` pairs the two for everything downstream of a load —
the provider, the communication backends and the telemetry loop all read a mix
of both halves off one object.
"""

from dataclasses import dataclass

from gradys_embedded.communication import PROTOCOLS, ZENOH_PROTOCOLS


@dataclass
class RunnerConfiguration:
    """Provisioned, machine-bound settings — everything needed to boot idle.

    Set once by gradys-fleet for the (image, network) pair. Stable for as long
    as a drone sits on a given LAN. Mission parameters (peer map, frame,
    transport, initial position) are deliberately NOT here: they arrive with
    each ``POST /mission/load`` as a :class:`MissionConfiguration`.
    """

    node_id: int

    uav_api_port: int
    """Port for the local UAV HTTP API"""

    control_api_port: int
    """Dedicated HTTP port for the control plane (`/mission`, `/protocols`, `/runs`).

    Served over plain HTTP regardless of the mission's transport. The data-plane
    message API is served separately on `data_port`, so the two never share a
    port. Must be unique per node when several nodes run on the same host."""

    data_port: int
    """Port this node binds for the inter-node data plane.

    Which transport serves it is chosen per mission at `POST /mission/load`;
    the port itself is a property of the machine. Required: the peer map is
    mission-supplied, so nothing else can provide the bind port."""

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
    auto-generates one. Ignored when the mission's transport is "http" or "zenoh_tcp"."""

    keyfile: str | None = None
    """TLS private key (PEM) paired with certfile. Required when certfile is provided. Ignored when
    the mission's transport is "http"/"zenoh_tcp" or when certfile is None."""

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

    def __post_init__(self) -> None:
        # Expanded here rather than at each use site, so a config written as
        # "~/gradys_runs" behaves the same whether it came from TOML or was
        # constructed in Python.
        import os

        self.runs_dir = os.path.expanduser(self.runs_dir)
        self.protocols_dir = os.path.expanduser(self.protocols_dir)


@dataclass
class MissionConfiguration:
    """Everything that describes a run rather than a machine.

    Carried by ``POST /mission/load`` — the only gateway; the TOML loader
    rejects every one of these keys. Every field except `initial_position` must
    be IDENTICAL across the fleet, and nothing here can validate that, which is
    why `/mission/status` echoes them for the caller to check before starting.
    """

    node_ip_dict: dict[int, str] | None = None
    """Maps node_id to 'ip:port' string for the message API (e.g. '192.168.1.10:8000').

    Bare `host:port`, no scheme. Supplying it per mission is what lets the fleet
    gain or lose a drone without re-provisioning every other one. Required at
    load unless the transport is zenoh with auto_scout, which discovers peers."""

    initial_position: tuple[float, float, float] | None = None
    """Initial position for the UAV in Cartesian Coordinates (x, y, z).

    The z component doubles as the takeoff altitude. Must be set before
    `/mission/setup` can fly the drone anywhere."""

    origin_gps_coordinates: tuple[float, float, float] | None = None
    """Origin GPS coordinates (latitude, longitude, altitude) for converting between GPS and
    Cartesian coordinates. If None, the drone's own position at mission load is used as the
    origin — which puts every drone in a different frame unless the whole fleet is given the
    same explicit value."""

    x_axis_degrees: float | None = None
    """Clockwise rotation of the protocol's x-axis from true north, in degrees. The y-axis is
    always 90 degrees clockwise from the x-axis; 0.0 keeps the NEU convention (x=North, y=East).
    If None, it is initialized from the drone's own heading at mission load. Must be identical on
    every node in the fleet — mismatched rotations silently desynchronize cartesian frames."""

    communication_protocol: str = "http"
    """Transport for the inter-node message API. Chosen per mission; only the
    chosen one is ever served, and the data plane binds nothing until a mission
    loads. A mission on a different transport rebinds the same `data_port`;
    consecutive missions on the same one reuse the listener.

    One of:
    - "http" (default): HTTP/1.1 over TCP via uvicorn, plain (no TLS).
    - "https": HTTP/1.1 over TLS via uvicorn, using the provisioned certfile/keyfile (or an
      ephemeral self-signed cert).
    - "http3": HTTP/3 over QUIC (UDP) via Hypercorn, TLS 1.3; requires `pip install
      "gradys-embedded[http3]"`.
    - "zenoh_tcp": Eclipse Zenoh pub/sub in peer (p2p) mode over TCP links (no TLS); requires
      `pip install "gradys-embedded[zenoh]"`. See `auto_scout`.
    - "zenoh_quic": Eclipse Zenoh pub/sub in peer (p2p) mode over QUIC links (TLS 1.3); requires
      `pip install "gradys-embedded[zenoh]"`. Needs a fleet-wide provisioned certfile/keyfile
      (identical on every node) or load rejects it.
    Must be identical on every node in the fleet — mixed transports cannot interoperate.
    Unrelated to uav_api's own --udp flag; the local uav_api connection always uses plain HTTP
    on localhost."""

    auto_scout: bool = False
    """Whether nodes discover each other automatically instead of using the static `node_ip_dict`.

    Only implemented for the zenoh transports (`zenoh_tcp`, `zenoh_quic`):
    - False (default): Zenoh peer mode with multicast disabled and explicit `connect.endpoints`
      built from `node_ip_dict` (works on multicast-blocked LANs; keeps `node_ip_dict` authoritative).
    - True: Zenoh peer mode using default UDP multicast scouting (224.0.0.224:7446); peers
      auto-discover and `node_ip_dict` is not needed for transport.
    Has NO effect for "http"/"https"/"http3" (those transports have no discovery); it is accepted
    but ignored for them."""

    telemetry_interval: float = 0.5
    """Seconds between telemetry polls while this mission is in force. While no
    mission is loaded the service polls at its own fixed idle rate."""

    def __post_init__(self) -> None:
        if self.node_ip_dict is not None:
            peers: dict[int, str] = {}
            for key, value in self.node_ip_dict.items():
                try:
                    node = int(key)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"node_ip_dict key {key!r} is not an integer node id"
                    ) from None
                address = str(value)
                # The send path builds f"http://{addr}/message", so a scheme here
                # yields "http://http://..." and every send fails silently.
                if "://" in address:
                    raise ValueError(
                        f"node_ip_dict[{node}] = {address!r} must not include a scheme; "
                        f"use the bare form \"host:port\"."
                    )
                peers[node] = address
            self.node_ip_dict = peers

        if self.initial_position is not None:
            self.initial_position = tuple(self.initial_position)
        if self.origin_gps_coordinates is not None:
            self.origin_gps_coordinates = tuple(self.origin_gps_coordinates)
        if self.x_axis_degrees is not None:
            self.x_axis_degrees = float(self.x_axis_degrees)

        if self.communication_protocol not in PROTOCOLS:
            raise ValueError(
                f"Invalid communication_protocol {self.communication_protocol!r}; "
                f"must be one of {sorted(PROTOCOLS)}"
            )

        if self.telemetry_interval <= 0:
            raise ValueError(
                f"telemetry_interval must be positive, got {self.telemetry_interval!r}"
            )

        if self.auto_scout and self.communication_protocol not in ZENOH_PROTOCOLS:
            import logging

            logging.getLogger(__name__).warning(
                "auto_scout=True has no effect for communication_protocol=%r; it is only "
                "implemented for the zenoh transports. Ignoring.",
                self.communication_protocol,
            )


@dataclass(frozen=True)
class MissionContext:
    """One mission's effective configuration: the machine plus the experiment.

    Everything downstream of a load (provider, backends, telemetry loop) reads a
    mix of provisioned and mission fields off one object; the passthrough
    properties keep those consumers unchanged by the split. Frozen: to amend a
    mission (frame resolution), build a new context around a `dataclasses.replace`
    of the `mission` half — never mutate one in place.
    """

    provisioned: RunnerConfiguration
    mission: MissionConfiguration

    # --- provisioned half -----------------------------------------------------

    @property
    def node_id(self) -> int:
        return self.provisioned.node_id

    @property
    def uav_api_port(self) -> int:
        return self.provisioned.uav_api_port

    @property
    def data_port(self) -> int:
        return self.provisioned.data_port

    @property
    def certfile(self) -> str | None:
        return self.provisioned.certfile

    @property
    def keyfile(self) -> str | None:
        return self.provisioned.keyfile

    # --- mission half ---------------------------------------------------------

    @property
    def node_ip_dict(self) -> dict[int, str] | None:
        return self.mission.node_ip_dict

    @property
    def initial_position(self) -> tuple[float, float, float] | None:
        return self.mission.initial_position

    @property
    def origin_gps_coordinates(self) -> tuple[float, float, float] | None:
        return self.mission.origin_gps_coordinates

    @property
    def x_axis_degrees(self) -> float | None:
        return self.mission.x_axis_degrees

    @property
    def communication_protocol(self) -> str:
        return self.mission.communication_protocol

    @property
    def auto_scout(self) -> bool:
        return self.mission.auto_scout

    @property
    def telemetry_interval(self) -> float:
        return self.mission.telemetry_interval

from dataclasses import dataclass

from gradys_embedded.protocol.position import Position


@dataclass
class Telemetry:
    current_position: Position
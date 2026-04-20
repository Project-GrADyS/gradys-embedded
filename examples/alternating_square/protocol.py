import logging

from gradys_embedded.encapsulator.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.communication import BroadcastMessageCommand, SendMessageCommand

READY_INTERVAL = 0.5
WP_TOLERANCE = 2
mission_list = [
    (20, 0, 10),
    (20, 20, 10),
    (0, 20, 10),
    (0, 0, 10),
]

class AlternatingSquareProtocol(IProtocol):
    _log: logging.Logger
    _wp_id: int
    _state: str
    def initialize(self) -> None:
        self._log = logging.getLogger()
        self._state = "READY"
        if self.provider.get_id() == 1:
            self._wp_id = 0
        else:
            self._wp_id = 2

    def handle_timer(self, timer: str) -> None:
        if timer == "ready":
            if self._state != "READY":
                return
            
            self._log.info("Sending ready heartbeat")
            self.provider.send_communication_command(BroadcastMessageCommand(
                message=f"READY,{self._wp_id}"
            ))
            self.provider.schedule_timer("ready", self.provider.current_time() + READY_INTERVAL)

    def handle_packet(self, message: str) -> None:
        self._log.info(f"Received message: {message}")
        if message.startswith("READY"):
            _, wp_id_str = message.split(",")
            partner_wp_id = int(wp_id_str)
            if (partner_wp_id%2) == (self._wp_id%2):
                self._log.info(f"Received ready from partner, going to next waypoint")
                self._state = "MOVING"
                self.provider.send_mobility_command()

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        current_wp_id = self._wp_id-1

        if current_wp_id is None:
            return
        
        current_wp = mission_list[current_wp_id]
        error = ((telemetry.current_position[0] - current_wp[0]) ** 2 +
                 (telemetry.current_position[1] - current_wp[1]) ** 2 +
                 (telemetry.current_position[2] - current_wp[2]) ** 2) ** 0.5
        self._log.info(f"Current position: {telemetry.current_position}, current waypoint: {current_wp}, error: {error}")

        if error < WP_TOLERANCE and self._state == "MOVING":
            self._log.info(f"Reached waypoint {current_wp_id}, sending ready")
            self._state = "READY"
            self.provider.schedule_timer("ready", self.provider.current_time() + READY_INTERVAL)

    def finish(self) -> None:
        self._log.info(f"Final packet count: {self.packet_count}")

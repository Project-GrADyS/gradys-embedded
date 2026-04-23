import logging

from gradys_embedded.encapsulator.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.mobility import GotoCoordsMobilityCommand
from gradys_embedded.protocol.messages.communication import SendMessageCommand

WAITING_INTERVAL = 5
ORIGINAL_POINT = (0,0,2)
FAR_POINT = (3,0,2)
WP_TOLERANCE = 1

class BackAndForthProtocol(IProtocol):
    _log: logging.Logger
    _in_original_point: bool
    _state: str
    _current_wp: tuple[float, float, float]

    def initialize(self) -> None:
        self._log = logging.getLogger()
        self._state = "WAITING"
        self._current_wp = FAR_POINT

        self.provider.schedule_timer("goto", self.provider.current_time() + WAITING_INTERVAL)
    def handle_timer(self, timer: str) -> None:
        if timer == "goto":
            if self._state != "WAITING":
                return
            
            self._state = "READY"
            self.provider.send_communication_command(SendMessageCommand(
                message=f"GOTO", 
                destination=0
            ))
            self._log.info("Ready to move.")

    def handle_packet(self, message: str) -> None:
        self._log.info(f"Received message: {message}")
        if message.startswith("GOTO") and self._state == "READY":
            self._log.info(f"Received goto message, moving to far point")
            self._state = "MOVING"
            self.provider.send_mobility_command(GotoCoordsMobilityCommand(*self._current_wp))

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        if self._state != "MOVING":
            return

        error = ((telemetry.current_position[0] - self._current_wp[0]) ** 2 +
                 (telemetry.current_position[1] - self._current_wp[1]) ** 2 +
                 (telemetry.current_position[2] - self._current_wp[2]) ** 2) ** 0.5
        self._log.info(f"Current position: {telemetry.current_position}, current waypoint: {self._current_wp}, error: {error}")

        if error < WP_TOLERANCE:
            self._log.info(f"Reached waypoint {self._current_wp}, switching to waiting state")
            self._state = "WAITING"
            self._current_wp = ORIGINAL_POINT if self._current_wp == FAR_POINT else FAR_POINT
            self.provider.schedule_timer("goto", self.provider.current_time() + WAITING_INTERVAL)

    def finish(self) -> None:
        self._log.info(f"Finilazing")

import logging

from gradys_embedded.encapsulator.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.mobility import GotoCoordsMobilityCommand, SetSpeedMobilityCommand
from gradys_embedded.protocol.messages.communication import SendMessageCommand
from gradys_embedded.protocol.plugin.follow_mobility import MobilityLeaderPlugin, MobilityLeaderConfiguration, MobilityFollowerPlugin, MobilityFollowerConfiguration
WAITING_INTERVAL = 5
ORIGINAL_POINT = (0,0,4)
FAR_POINT = (10,0,4)
WP_TOLERANCE = 1

class LeaderProtocol(IProtocol):
    _log: logging.Logger
    _state: str
    _current_wp: tuple[float, float, float]
    _mobility_leader: MobilityLeaderPlugin

    def initialize(self) -> None:
        self._log = logging.getLogger()
        self._state = "WAITING"
        self._current_wp = FAR_POINT

        self._mobility_leader = MobilityLeaderPlugin(protocol=self, configuration=MobilityLeaderConfiguration(
            broadcast_interval=0.5,
            follower_timeout=10
        ))

        self.provider.send_mobility_command(SetSpeedMobilityCommand(2))

        self.provider.schedule_timer("goto", self.provider.current_time() + WAITING_INTERVAL)
    def handle_timer(self, timer: str) -> None:
        if timer == "goto":
            if self._state != "WAITING":
                return
            
            self._log.info(f"Moving...")
            self._state = "MOVING"
            self.provider.send_mobility_command(GotoCoordsMobilityCommand(*self._current_wp))

    def handle_packet(self, message: str) -> None:
        pass

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

class FollowerProtocol(IProtocol):
    _log: logging.Logger
    _mobility_follower: MobilityFollowerPlugin

    def initialize(self):
        self.provider.send_mobility_command(SetSpeedMobilityCommand(4))

        self._log = logging.getLogger()
        self._mobility_follower = MobilityFollowerPlugin(protocol=self, configuration=MobilityFollowerConfiguration(
            scanning_interval=1,
            leader_timeout=5
        ))
        self._mobility_follower.set_relative_position((-2,-2,-2))

    def handle_timer(self, timer: str) -> None:
        pass

    def handle_packet(self, message: str) -> None:
        pass

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        pass

    def finish(self) -> None:
        pass
import enum
import logging
import json

from typing import TypedDict

from gradys_embedded.encapsulator.interface import IProtocol
from gradys_embedded.protocol.messages.telemetry import Telemetry
from gradys_embedded.protocol.messages.communication import BroadcastMessageCommand
from gradys_embedded.protocol.plugin.mission_mobility import MissionMobilityPlugin, MissionMobilityConfiguration, LoopMission


mission_list = [
    (0, 0, 20),
    (150, 0, 20),
    (0, 0, 20),
    (0, 150, 20),
    (0, 0, 20),
    (-150, 0, 20),
    (0, 0, 20),
    (0, -150, 20)
]

class SimpleMessage(TypedDict):
    packet_count: int
    sender_type: int
    sender: int

class SimpleSender(enum.Enum):
    SENSOR = 0
    UAV = 1
    GROUND_STATION = 2 

def report_message(message: SimpleMessage) -> str:
    return (f"Received message with {message['packet_count']} packets from "
            f"{SimpleSender(message['sender_type']).name} {message['sender']}")

class SimpleUAVProtocol(IProtocol):
    _log: logging.Logger

    packet_count: int

    _mission: MissionMobilityPlugin

    def initialize(self) -> None:
        self._log = logging.getLogger()
        self.packet_count = 0

        self._mission = MissionMobilityPlugin(self, MissionMobilityConfiguration(
            loop_mission=LoopMission.REVERSE,
            tolerance=2
        ))

        self._mission.start_mission(mission_list)

        self._send_heartbeat()

    def _send_heartbeat(self) -> None:
        self._log.info(f"Sending heartbeat, current count {self.packet_count}")

        message: SimpleMessage = {
            'packet_count': self.packet_count,
            'sender_type': SimpleSender.UAV.value,
            'sender': self.provider.get_id()
        }
        command = BroadcastMessageCommand(json.dumps(message))
        self.provider.send_communication_command(command)

        self.provider.schedule_timer("", self.provider.current_time() + 1)

    def handle_timer(self, timer: str) -> None:
        self._send_heartbeat()

    def handle_packet(self, message: str) -> None:
        simple_message: SimpleMessage = json.loads(message)
        self._log.info(report_message(simple_message))

        if simple_message['sender_type'] == SimpleSender.SENSOR.value:
            self.packet_count += simple_message['packet_count']
            self._log.info(f"Received {simple_message['packet_count']} packets from "
                           f"sensor {simple_message['sender']}. Current count {self.packet_count}")
        elif simple_message['sender_type'] == SimpleSender.GROUND_STATION.value:
            self._log.info("Received acknowledgment from ground station")
            self.packet_count = 0

    def handle_telemetry(self, telemetry: Telemetry) -> None:
        current_wp_id = self._mission.current_waypoint

        if current_wp_id is None:
            return
        
        current_wp = mission_list[current_wp_id]
        error = ((telemetry.current_position[0] - current_wp[0]) ** 2 +
                 (telemetry.current_position[1] - current_wp[1]) ** 2 +
                 (telemetry.current_position[2] - current_wp[2]) ** 2) ** 0.5
        self._log.info(f"Current position: {telemetry.current_position}, current waypoint: {current_wp}, error: {error}")

    def finish(self) -> None:
        self._log.info(f"Final packet count: {self.packet_count}")

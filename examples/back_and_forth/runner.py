"""Starts the mission service for the back-and-forth example.

Only machine-bound settings live here; the mission itself (protocol, peer map,
initial position, frame) is loaded over HTTP -- see mission.sh next to this
file. `protocol:BackAndForthProtocol` resolves without an upload because
running this script puts its directory on sys.path.
"""

from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=0,
        uav_api_port=8000,
        control_api_port=6000,
        data_port=5000,
    )
    runner = EmbeddedRunner(runner_configuration)
    runner.start_api()

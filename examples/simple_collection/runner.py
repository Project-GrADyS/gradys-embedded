"""Starts the mission service for one UAV of the simple-collection example.

Only machine-bound settings live here; the mission itself (protocol, peer map,
frame, initial position) is loaded over HTTP -- see mission.sh next to this
file.
"""

from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=1,
        uav_api_port=8000,
        control_api_port=6000,
        data_port=5000,
    )
    runner = EmbeddedRunner(runner_configuration)
    runner.start_api()

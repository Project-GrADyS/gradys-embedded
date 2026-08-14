"""Starts the mission service for the leader drone. See mission.sh for the
mission itself -- both drones are loaded before either starts."""

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

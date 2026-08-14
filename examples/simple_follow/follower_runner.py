"""Starts the mission service for the follower drone. See mission.sh for the
mission itself -- both drones are loaded before either starts."""

from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=1,
        uav_api_port=8001,
        control_api_port=6001,
        data_port=5001,
    )
    runner = EmbeddedRunner(runner_configuration)
    runner.start_api()

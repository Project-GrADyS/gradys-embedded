from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

from protocol import LeaderProtocol

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=0,
        node_ip_dict={
            0: "localhost:5000",
            1: "localhost:5001"
        },
        uav_api_port=8000,
        origin_gps_coordinates=None,  # Use current UAV position as origin
        x_axis_degrees=None,
        initial_position=(0, 0, 4)
    )
    runner = EmbeddedRunner(runner_configuration, LeaderProtocol)
    runner.start_api()
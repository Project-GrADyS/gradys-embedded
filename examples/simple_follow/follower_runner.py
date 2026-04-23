from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

from protocol import FollowerProtocol

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=1,
        node_ip_dict={
            0: "localhost:5000",
            1: "localhost:5001"
        },
        uav_api_port=8001,
        origin_gps_coordinates=None,  # Use current UAV position as origin
        x_axis_degrees=None,
        initial_position=(-2, -2, 2)
    )
    runner = EmbeddedRunner(runner_configuration, FollowerProtocol)
    runner.start_api()
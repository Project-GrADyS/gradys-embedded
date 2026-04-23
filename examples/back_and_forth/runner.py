from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

from protocol import BackAndForthProtocol

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=0,
        node_ip_dict={
            0: "http://localhost:5000",
        },
        uav_api_port=8000,
        #origin_gps_coordinates=(-15.840081, -47.926642, -0.016),
        origin_gps_coordinates=None,  # Use current UAV position as origin
        initial_position=(0, 0, 2)
    )
    runner = EmbeddedRunner(runner_configuration, BackAndForthProtocol)
    runner.start_api()
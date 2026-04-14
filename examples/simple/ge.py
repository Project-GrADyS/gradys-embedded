from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

from protocol import SimpleUAVProtocol

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=1,
        node_ip_dict={
            1: "http://localhost:5000"
        },
        uav_api_port=8000,
        origin_gps_coordinates=(-15.840081, -47.926642, -0.016),
    )
    runner = EmbeddedRunner(runner_configuration, SimpleUAVProtocol)
    runner.run()
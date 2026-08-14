from gradys_embedded.runner.runner import EmbeddedRunner
from gradys_embedded.runner.configuration import RunnerConfiguration

from protocol import SimpleUAVProtocol

if __name__ == "__main__":
    runner_configuration = RunnerConfiguration(
        node_id=1,
        # Bare "host:port" -- no scheme. The transport builds
        # f"http://{addr}/message" itself, so a scheme here yields
        # "http://http://..." and every send fails silently.
        node_ip_dict={
            1: "localhost:5000",
            2: "localhost:5001",
            3: "localhost:5002",
            4: "localhost:5003",
            5: "localhost:5004",
        },
        uav_api_port=8000,
        control_api_port=6000,
        origin_gps_coordinates=(-15.840081, -47.926642, -0.016),
        initial_position=(0, 0, 20)
    )
    runner = EmbeddedRunner(runner_configuration, SimpleUAVProtocol)
    runner.start_api()
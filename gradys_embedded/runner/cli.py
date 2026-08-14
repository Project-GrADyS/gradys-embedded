"""Command-line entry point: ``gradys-embedded --config <file.toml>``.

The configuration file carries only what is bound to the machine. Everything
that describes an experiment -- the protocol, the peer map, the coordinate
frame, the transport -- arrives per mission over HTTP (``POST /mission/load``),
so a deployment ships a data file and the service boots idle.
"""

import argparse
import sys

from gradys_embedded.runner.config_file import ConfigurationError, load_configuration
from gradys_embedded.runner.runner import EmbeddedRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gradys-embedded",
        description="Run the GrADyS mission service on a drone against a local uav_api.",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help="Path to the TOML runner configuration.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        configuration = load_configuration(args.config)
    except ConfigurationError as exc:
        print(f"gradys-embedded: {exc}", file=sys.stderr)
        return 2

    EmbeddedRunner(configuration).start_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

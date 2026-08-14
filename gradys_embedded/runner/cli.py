"""Command-line entry point: ``gradys-embedded --config <file.toml>``.

The protocol stays user code. It is named in the configuration (or on the
command line) as an import path, ``module:ClassName``, and resolved at startup
from ``PYTHONPATH`` -- so a deployment ships its protocol module and a data file
rather than a generated runner script.
"""

import argparse
import importlib
import sys

from gradys_embedded.protocol.interface import IProtocol
from gradys_embedded.runner.config_file import ConfigurationError, load_configuration
from gradys_embedded.runner.runner import EmbeddedRunner


def resolve_protocol(spec: str) -> type[IProtocol]:
    """Import a protocol class from a ``module:ClassName`` specification."""
    if ":" not in spec:
        raise ConfigurationError(
            f"Protocol {spec!r} must be given as \"module:ClassName\" "
            f"(for example \"protocol:MyProtocol\")"
        )

    module_name, _, class_name = spec.partition(":")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigurationError(
            f"Could not import protocol module {module_name!r}: {exc}. "
            f"Is it on PYTHONPATH?"
        ) from exc

    try:
        protocol = getattr(module, class_name)
    except AttributeError:
        raise ConfigurationError(
            f"Module {module_name!r} has no attribute {class_name!r}"
        ) from None

    if not (isinstance(protocol, type) and issubclass(protocol, IProtocol)):
        raise ConfigurationError(
            f"{spec} is not an IProtocol subclass"
        )

    return protocol


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gradys-embedded",
        description="Run a GrADyS protocol on a drone against a local uav_api.",
    )
    parser.add_argument(
        "--config",
        required=True,
        metavar="FILE",
        help="Path to the TOML runner configuration.",
    )
    parser.add_argument(
        "--protocol",
        metavar="module:ClassName",
        help=(
            "Default protocol, overriding the `protocol` key in the configuration "
            "file. NOT started automatically -- the service boots idle and runs a "
            "protocol only when one is loaded over HTTP."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        configuration, configured_protocol = load_configuration(args.config)
        spec = args.protocol or configured_protocol
        # A protocol is optional. The service boots idle and waits for a mission
        # over HTTP; naming one here only makes it the default that the legacy
        # /protocol/setup path will pick up.
        protocol = resolve_protocol(spec) if spec else None
    except ConfigurationError as exc:
        print(f"gradys-embedded: {exc}", file=sys.stderr)
        return 2

    EmbeddedRunner(configuration, protocol).start_api()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

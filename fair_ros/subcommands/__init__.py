"""ros2 fairy verb implementations.

Each module exposes a plain ``run(args, console) -> int`` (unit-testable
without ROS) plus a thin ros2cli VerbExtension wrapper. The shim below lets
the modules import in environments without ros2cli (CI, unit tests).
"""

import logging
import sys

try:
    from ros2cli.verb import VerbExtension
except ImportError:  # pragma: no cover - exercised only outside ROS
    class VerbExtension:  # type: ignore[no-redef]
        """Stand-in with the same interface as ros2cli's VerbExtension."""

        def add_arguments(self, parser, cli_name):
            pass


def _configure_logging(debug: bool) -> None:
    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
            stream=sys.stderr,
        )


def guarded_main(run, args) -> int:
    """The verb boundary: nothing past here may show an operator a traceback.

    Verbs handle their *known* failures with plain language; this catches the
    unexpected rest (no stack traces in normal flow) and turns
    Ctrl-C into the mandated exit 130. ``--debug`` still gets the full
    traceback on stderr — that flag marks an engineer.
    """
    try:
        return run(args)
    except KeyboardInterrupt:
        print("\nCancelled — nothing was changed.", file=sys.stderr)
        return 130
    except Exception:
        if getattr(args, "debug", False):
            raise
        print("Sorry — something went wrong that fair-ros didn't expect.\n"
              "Nothing is lost: your recordings and saved missions are not\n"
              "touched by this error. Re-run the same command with --debug\n"
              "and share the output with your robot engineer.",
              file=sys.stderr)
        return 1

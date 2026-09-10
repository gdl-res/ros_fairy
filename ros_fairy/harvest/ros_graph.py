"""ROS graph snapshot via ros2 CLI subprocesses only (no rclpy).

Commands used: ros2 node list, ros2 topic list -t, ros2 pkg list,
ros2 param dump <node>. Keeping to subprocess keeps this module portable
across ROS 2 distros.
"""

import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import yaml

ROS2_CLI_TIMEOUT_S = 20
PARAM_DUMP_BUDGET_S = 60


class RosGraphError(Exception):
    """ros2 CLI was unreachable or failed."""


def _run(args: list[str], timeout: float = ROS2_CLI_TIMEOUT_S) -> str:
    try:
        result = subprocess.run(
            ["ros2", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise RosGraphError("ros2 CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RosGraphError(f"'ros2 {args[0]}' timed out") from exc
    if result.returncode != 0:
        raise RosGraphError(
            f"'ros2 {' '.join(args)}' failed: {result.stderr.strip()}")
    return result.stdout


def list_nodes() -> list[str]:
    return sorted(line.strip() for line in _run(["node", "list"]).splitlines()
                  if line.strip())


def list_topics() -> list[dict[str, str]]:
    """Parse 'ros2 topic list -t' lines of the form '/name [pkg/msg/Type]'."""
    topics = []
    for line in _run(["topic", "list", "-t"]).splitlines():
        line = line.strip()
        if not line:
            continue
        name, _, rest = line.partition(" ")
        topics.append({"name": name, "type": rest.strip().strip("[]")})
    return sorted(topics, key=lambda t: t["name"])


def list_packages() -> list[str]:
    return sorted(line.strip() for line in _run(["pkg", "list"]).splitlines()
                  if line.strip())


def dump_params(node: str, timeout: float = ROS2_CLI_TIMEOUT_S) -> dict:
    parsed = yaml.safe_load(_run(["param", "dump", node], timeout=timeout))
    return parsed if isinstance(parsed, dict) else {}


def harvest() -> dict[str, Any]:
    """Full graph snapshot.

    Raises RosGraphError only if the basic listing commands fail (ROS down).
    Individual param dump failures degrade to complete=False instead.
    """
    nodes = list_nodes()
    topics = list_topics()
    packages = list_packages()

    parameters: dict[str, dict] = {}
    complete = True
    deadline = time.monotonic() + PARAM_DUMP_BUDGET_S
    for node in nodes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            complete = False
            break
        try:
            parameters[node] = dump_params(
                node, timeout=min(ROS2_CLI_TIMEOUT_S, remaining))
        except RosGraphError:
            complete = False

    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "nodes": nodes,
        "topics": topics,
        "ros_packages": packages,
        "parameters": parameters,
        "complete": complete,
    }

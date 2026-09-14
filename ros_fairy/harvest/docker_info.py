"""Snapshot of running Docker containers. Graceful no-op without Docker."""

import json
import subprocess
import time
from typing import Any

DOCKER_TIMEOUT_S = 10
# `ros2 pkg list` exec'd into a container can legitimately take a few
# seconds (spawning python, scanning the ament index) — this budget is
# deliberately separate from DOCKER_TIMEOUT_S (which covers the quick `ps`/
# `inspect` calls) so probing containers' package lists can never starve
# the rest of the docker harvest, the same starvation bug that used to hit
# harvest/ros_graph.py's per-node param dumps.
CONTAINER_EXEC_TIMEOUT_S = 10
CONTAINER_PKG_LIST_BUDGET_S = 30

_COMPOSE_PROJECT = "com.docker.compose.project"
_COMPOSE_FILES = "com.docker.compose.project.config_files"


def _run(args: list[str], timeout: float) -> str | None:
    try:
        result = subprocess.run(
            ["docker", *args], capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def harvest() -> dict[str, Any]:
    """Return {docker_containers: [...], raw_inspect: [...], available: bool}.

    Never raises: any Docker problem yields an empty result with
    available=False so the watchdog records status 'skipped'.
    """
    empty = {"docker_containers": [], "raw_inspect": [], "available": False}
    deadline = time.monotonic() + DOCKER_TIMEOUT_S

    ps = _run(["ps", "-q"], timeout=DOCKER_TIMEOUT_S)
    if ps is None:
        return empty
    ids = [line.strip() for line in ps.splitlines() if line.strip()]
    if not ids:
        return {**empty, "available": True}

    inspect_out = _run(["inspect", *ids],
                       timeout=max(1.0, deadline - time.monotonic()))
    if inspect_out is None:
        return empty
    try:
        raw = json.loads(inspect_out)
    except json.JSONDecodeError:
        return empty

    # Separate budget from `deadline` above — see CONTAINER_PKG_LIST_BUDGET_S.
    pkg_deadline = time.monotonic() + CONTAINER_PKG_LIST_BUDGET_S

    containers = []
    for entry in raw:
        config = entry.get("Config") or {}
        labels = config.get("Labels") or {}
        running = (entry.get("State") or {}).get("Running") is True
        container_id = entry.get("Id")
        # docker inspect puts RepoDigests on image objects, not containers;
        # for containers we re-resolve from .Image via a dedicated inspect.
        containers.append({
            "name": (entry.get("Name") or "").lstrip("/"),
            "image": config.get("Image") or "",
            "digest": _image_digest(entry.get("Image"), deadline),
            "compose_project": labels.get(_COMPOSE_PROJECT),
            "compose_file": labels.get(_COMPOSE_FILES),
            "ros_packages": _container_ros_packages(container_id, pkg_deadline)
                if running and container_id else None,
        })
    return {"docker_containers": containers, "raw_inspect": raw,
            "available": True}


def _container_ros_packages(container_id: str, deadline: float
                            ) -> list[str] | None:
    """Best-effort `ros2 pkg list` run inside a running container.

    A robot whose ROS stack lives entirely in Docker — nothing installed on
    the host — otherwise gets a package list that reflects the wrong
    environment (see harvest/ros_graph.py's list_packages(), which only ever
    sees the host). Two attempts, mirroring what an operator doing
    `docker exec -it <container> bash` and then `ros2 pkg list` would see:
    a bare exec first (works when the image bakes ROS env vars into its
    Dockerfile ENV — `docker exec` inherits those), then a login+interactive
    shell (sources ~/.bashrc, where hand-rolled robot images more commonly
    put the `source .../setup.bash` line instead). Never raises; None means
    "couldn't tell" (no Docker, container has no ROS, both attempts failed —
    all indistinguishable from here), not "zero packages".
    """
    for cmd in (["ros2", "pkg", "list"], ["bash", "-ic", "ros2 pkg list"]):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        out = _run(["exec", container_id, *cmd],
                   timeout=min(CONTAINER_EXEC_TIMEOUT_S, remaining))
        if out is not None:
            packages = sorted(
                line.strip() for line in out.splitlines() if line.strip())
            return packages or None
    return None


def _image_digest(image_id: str | None, deadline: float) -> str | None:
    if not image_id:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    out = _run(["inspect", "--format", "{{json .RepoDigests}}", image_id],
               timeout=remaining)
    if out is None:
        return None
    try:
        digests = json.loads(out)
    except json.JSONDecodeError:
        return None
    return digests[0] if isinstance(digests, list) and digests else None

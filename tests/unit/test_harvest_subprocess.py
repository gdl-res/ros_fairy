"""Tests for the subprocess-driven harvest modules with mocked subprocess."""

import json
import subprocess
from unittest import mock

import pytest

from fair_ros.harvest import docker_info, ros_graph, system_info
from fair_ros.harvest.ros_graph import RosGraphError


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- ros_graph ---------------------------------------------------------------

NODE_LIST = "/navsat\n/controller\n"
TOPIC_LIST = "/fix [sensor_msgs/msg/NavSatFix]\n/depth [ping_msgs/msg/Ping]\n"
PKG_LIST = "rclpy\nnav2_core\n"
PARAM_DUMP = "/navsat:\n  ros__parameters:\n    rate: 5.0\n"


def test_ros_graph_harvest():
    def fake_run(cmd, **kw):
        out = {"node": NODE_LIST, "topic": TOPIC_LIST,
               "pkg": PKG_LIST, "param": PARAM_DUMP}[cmd[1]]
        return _completed(out)

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = ros_graph.harvest()

    assert data["nodes"] == ["/controller", "/navsat"]
    assert {"name": "/fix", "type": "sensor_msgs/msg/NavSatFix"} in data["topics"]
    assert data["ros_packages"] == ["nav2_core", "rclpy"]
    assert data["parameters"]["/navsat"]["/navsat"]["ros__parameters"]["rate"] == 5.0
    assert data["complete"] is True
    assert data["captured_at"]


def test_ros_graph_ros_down():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(RosGraphError, match="not found"):
            ros_graph.harvest()


def test_ros_graph_param_dump_failure_degrades():
    def fake_run(cmd, **kw):
        if cmd[1] == "param":
            return _completed("", returncode=1, stderr="boom")
        return _completed({"node": NODE_LIST, "topic": TOPIC_LIST,
                           "pkg": PKG_LIST}[cmd[1]])

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = ros_graph.harvest()
    assert data["complete"] is False
    assert data["parameters"] == {}


def test_ros_graph_timeout():
    with mock.patch("subprocess.run",
                    side_effect=subprocess.TimeoutExpired("ros2", 20)):
        with pytest.raises(RosGraphError, match="timed out"):
            ros_graph.list_nodes()


# --- docker_info -------------------------------------------------------------

INSPECT = [{
    "Name": "/navstack",
    "Image": "sha256:abc",
    "Config": {
        "Image": "example/navstack:1.4.2",
        "Labels": {
            "com.docker.compose.project": "robot",
            "com.docker.compose.project.config_files": "/opt/robot/compose.yml",
        },
    },
}]


def test_docker_harvest():
    def fake_run(cmd, **kw):
        if cmd[1] == "ps":
            return _completed("c0ffee\n")
        if "--format" in cmd:
            return _completed('["example/navstack@sha256:7be1"]')
        return _completed(json.dumps(INSPECT))

    with mock.patch("subprocess.run", side_effect=fake_run):
        data = docker_info.harvest()

    assert data["available"] is True
    c = data["docker_containers"][0]
    assert c["name"] == "navstack"
    assert c["image"] == "example/navstack:1.4.2"
    assert c["digest"] == "example/navstack@sha256:7be1"
    assert c["compose_project"] == "robot"
    assert c["compose_file"] == "/opt/robot/compose.yml"


def test_docker_absent():
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        data = docker_info.harvest()
    assert data == {"docker_containers": [], "raw_inspect": [],
                    "available": False}


def test_docker_no_containers():
    with mock.patch("subprocess.run", return_value=_completed("\n")):
        data = docker_info.harvest()
    assert data["available"] is True
    assert data["docker_containers"] == []


# --- system_info -------------------------------------------------------------

def test_system_info(monkeypatch):
    monkeypatch.setenv("ROS_DISTRO", "jazzy")
    dpkg = _completed("ros-jazzy-rclpy 7.1.0\nros-jazzy-nav2 1.3.0\n")
    with mock.patch("subprocess.run", return_value=dpkg):
        data = system_info.harvest()
    assert data["ros_distro"] == "jazzy"
    assert data["apt_ros_versions"]["ros-jazzy-rclpy"] == "7.1.0"
    assert data["hostname"]
    assert data["kernel"].startswith("Linux")
    assert data["arch"]


def test_system_info_no_dpkg(monkeypatch):
    monkeypatch.delenv("ROS_DISTRO", raising=False)
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        data = system_info.harvest()
    assert data["ros_distro"] is None
    assert data["apt_ros_versions"] == {}


def test_system_info_records_clock_sync():
    dpkg = _completed("ros-jazzy-rclpy 7.1.0\n")
    with mock.patch("subprocess.run", return_value=dpkg), \
            mock.patch.object(system_info.clock, "is_synchronized",
                              return_value=False):
        data = system_info.harvest()
    # captured regardless of outcome so the archive shows the check ran (#27)
    assert data["clock_synchronized"] is False

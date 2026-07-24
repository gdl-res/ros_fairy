"""Unit tests for harvest/ros_descriptions with a fake rclpy.

The live `-m ros` smoke layer exercises the real thing; these cover the
capture, timeout, and no-rclpy paths in the default (CI) suite by injecting
stand-in modules for the imports harvest() performs.
"""

import sys
import types
from types import SimpleNamespace

from fair_ros.harvest import ros_descriptions


def _fake_transform():
    return SimpleNamespace(
        header=SimpleNamespace(frame_id="base_link"),
        child_frame_id="lidar_link",
        transform=SimpleNamespace(
            translation=SimpleNamespace(x=1.0, y=2.0, z=0.5),
            rotation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)))


class _FakeNode:
    def __init__(self):
        self.subs = {}

    def create_subscription(self, msg_type, topic, callback, qos):
        self.subs[topic] = callback

    def destroy_node(self):
        pass


class _FakeExecutor:
    """Delivers the latched messages on the first spin, like real DDS would."""

    def __init__(self, context=None, deliver=True):
        self._deliver = deliver
        self._node = None

    def add_node(self, node):
        self._node = node

    def spin_once(self, timeout_sec):
        if not self._deliver or self._node is None:
            return
        urdf = self._node.subs.get("/robot_description")
        if urdf:
            urdf(SimpleNamespace(data="<robot name='heron'/>"))
        tf = self._node.subs.get("/tf_static")
        if tf:
            tf(SimpleNamespace(transforms=[_fake_transform()]))

    def shutdown(self):
        pass


def _install_fake_rclpy(monkeypatch, deliver=True):
    node = _FakeNode()
    rclpy = types.ModuleType("rclpy")
    rclpy.Context = lambda: object()
    rclpy.init = lambda context=None: None
    rclpy.create_node = lambda name, context=None: node
    rclpy.shutdown = lambda context=None: None

    executors = types.ModuleType("rclpy.executors")
    executors.SingleThreadedExecutor = \
        lambda context=None: _FakeExecutor(context, deliver=deliver)

    qos = types.ModuleType("rclpy.qos")
    for name in ("DurabilityPolicy", "HistoryPolicy", "ReliabilityPolicy"):
        setattr(qos, name, SimpleNamespace(
            KEEP_LAST=1, RELIABLE=1, TRANSIENT_LOCAL=1))
    qos.QoSProfile = lambda **kwargs: SimpleNamespace(**kwargs)

    std_msgs = types.ModuleType("std_msgs.msg")
    std_msgs.String = object
    tf2_msgs = types.ModuleType("tf2_msgs.msg")
    tf2_msgs.TFMessage = object

    for name, module in [("rclpy", rclpy), ("rclpy.executors", executors),
                         ("rclpy.qos", qos), ("std_msgs.msg", std_msgs),
                         ("tf2_msgs.msg", tf2_msgs)]:
        monkeypatch.setitem(sys.modules, name, module)


def test_harvest_captures_urdf_and_tf(monkeypatch):
    _install_fake_rclpy(monkeypatch, deliver=True)
    result = ros_descriptions.harvest(timeout_s=1.0)
    assert result["robot_description"] == "<robot name='heron'/>"
    assert result["tf_static"] == [{
        "parent_frame": "base_link",
        "child_frame": "lidar_link",
        "translation": {"x": 1.0, "y": 2.0, "z": 0.5},
        "rotation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }]


def test_harvest_times_out_to_nones(monkeypatch):
    _install_fake_rclpy(monkeypatch, deliver=False)
    result = ros_descriptions.harvest(timeout_s=0.3)
    assert result == {"robot_description": None, "tf_static": None}


def test_harvest_without_rclpy_returns_nones(monkeypatch):
    monkeypatch.setitem(sys.modules, "rclpy", None)  # import -> ImportError
    result = ros_descriptions.harvest(timeout_s=0.3)
    assert result == {"robot_description": None, "tf_static": None}

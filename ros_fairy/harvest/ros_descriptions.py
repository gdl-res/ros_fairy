"""Grab /robot_description and /tf_static via a minimal rclpy node.

This is the only harvest module allowed to use rclpy:
both topics are transient-local latched publishers that subprocess tooling
cannot read reliably. Hard timeout budget; returns Nones on any problem,
including rclpy not being importable at all.
"""

import time
from typing import Any

# A fresh rclpy Context/node/DDS participant is created on every call (see
# harvest() below) — no pooling — so this budget has to cover DDS discovery
# from scratch, not just message delivery once matched. 5s occasionally
# wasn't enough for that even against a publisher that genuinely was up the
# whole time (observed 2026-09-11: a real robot_description publisher over
# Docker container networking timed out once, succeeded moments earlier/later
# with the same code) — a slower discovery handshake, not a QoS/topic-name
# mismatch, since a mismatch would fail every time, not intermittently.
RCLPY_TIMEOUT_S = 10


def harvest(timeout_s: float = RCLPY_TIMEOUT_S) -> dict[str, Any]:
    """Return {robot_description: str|None, tf_static: list[dict]|None}."""
    result: dict[str, Any] = {"robot_description": None, "tf_static": None}
    try:
        import rclpy
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import (
            DurabilityPolicy,
            HistoryPolicy,
            QoSProfile,
            ReliabilityPolicy,
        )
        from std_msgs.msg import String
        from tf2_msgs.msg import TFMessage
    except ImportError:
        return result

    latched = QoSProfile(
        depth=1,
        history=HistoryPolicy.KEEP_LAST,
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
    )

    context = None
    try:
        context = rclpy.Context()
        rclpy.init(context=context)
        node = rclpy.create_node("ros_fairy_harvest", context=context)
        # A private context needs its own executor: the module-level
        # rclpy.spin_once() would use the global executor bound to the
        # (uninitialised) default context.
        executor = SingleThreadedExecutor(context=context)
        executor.add_node(node)

        def on_urdf(msg):
            result["robot_description"] = msg.data

        def on_tf(msg):
            result["tf_static"] = [_transform_to_dict(t) for t in msg.transforms]

        node.create_subscription(String, "/robot_description", on_urdf, latched)
        node.create_subscription(TFMessage, "/tf_static", on_tf, latched)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.1)
            if result["robot_description"] is not None and \
                    result["tf_static"] is not None:
                break
        executor.shutdown()
        node.destroy_node()
    except Exception:
        pass
    finally:
        if context is not None:
            try:
                rclpy.shutdown(context=context)
            except Exception:
                pass
    return result


def _transform_to_dict(t) -> dict[str, Any]:
    tr, rot = t.transform.translation, t.transform.rotation
    return {
        "parent_frame": t.header.frame_id,
        "child_frame": t.child_frame_id,
        "translation": {"x": tr.x, "y": tr.y, "z": tr.z},
        "rotation": {"x": rot.x, "y": rot.y, "z": rot.z, "w": rot.w},
    }

"""Rendering tests for ui/diff.py (the rich table view of ros2 fairy diff).

The --json path is covered in test_subcommands; these exercise the rendered
sections, the added/removed/changed row convention, the graph-row cap, and
the no-differences case.
"""

import copy
import io

from rich.console import Console

from fair_ros.manifest import builder
from fair_ros.ui import diff as diff_ui
from tests.unit.test_archive import _spool


def _render(a, b) -> str:
    console = Console(file=io.StringIO(), width=140, force_terminal=False)
    diff_ui.show_diff(a, b, console=console)
    return console.file.getvalue()


def _pair(fair_dirs, mutate):
    """Two records from the same fixture spool; ``mutate(harvest, context)``
    shapes the second one."""
    h1, c1 = _spool(fair_dirs)
    a = builder.build(h1, c1)
    h2, c2 = copy.deepcopy(h1), copy.deepcopy(c1)
    mutate(h2, c2)
    return a, builder.build(h2, c2)


def test_identical_missions_show_no_differences(fair_dirs):
    a, b = _pair(fair_dirs, lambda h, c: None)
    out = _render(a, b)
    assert "No differences found." in out
    assert "Mission diff" in out


def test_context_changes_rendered(fair_dirs):
    def mutate(h, c):
        c["intent"]["goal"] = "Chart the harbour instead"
        c["identity"]["operator_name"] = "Marco"

    a, b = _pair(fair_dirs, mutate)
    out = _render(a, b)
    assert "Mission context" in out
    assert "Chart the harbour instead" in out
    assert "Marco" in out
    # unchanged sections are omitted entirely
    assert "Software" not in out


def test_software_and_graph_changes_rendered(fair_dirs):
    def mutate(h, c):
        h["software"]["ros_distro"] = "kilted"
        h["ros_graph"]["nodes"] = ["/navsat", "/lidar_driver"]
        h["ros_graph"]["topics"] = [
            {"name": "/scan", "type": "sensor_msgs/msg/LaserScan"}]

    a, b = _pair(fair_dirs, mutate)
    out = _render(a, b)
    assert "ROS graph" in out
    assert "/lidar_driver" in out       # added node
    assert "/fix" in out                # removed topic
    assert "kilted" in out


def test_graph_rows_capped_with_overflow_line(fair_dirs):
    def mutate(h, c):
        h["ros_graph"]["nodes"] = [f"/extra_{i:02d}" for i in range(30)]

    a, b = _pair(fair_dirs, mutate)
    out = _render(a, b)
    assert "more change" in out
    assert "/extra_29" not in out       # beyond the cap


def test_recording_changes_rendered(fair_dirs):
    def mutate(h, c):
        bag = h["bags"][0]
        bag["duration_s"] = (bag["duration_s"] or 0) + 300
        bag["size_bytes"] = bag["size_bytes"] * 2 + 1
        bag["health_warnings"] = [{
            "topic": "/fix", "sensor_id": "gps0", "kind": "gap",
            "start_offset_s": 1.0, "duration_s": 4.0,
            "plain_text": "GPS signal was lost for 4 seconds.",
        }]

    a, b = _pair(fair_dirs, mutate)
    out = _render(a, b)
    assert "Recordings" in out
    assert "Duration" in out
    assert "GPS signal was lost" in out


def test_diff_as_dict_only_contains_changed_sections(fair_dirs):
    def mutate(h, c):
        c["intent"]["goal"] = "Different"

    a, b = _pair(fair_dirs, mutate)
    data = diff_ui.diff_as_dict(a, b)
    assert set(data["changes"]) == {"mission_context"}
    assert data["mission_a"]["goal"] != data["mission_b"]["goal"]

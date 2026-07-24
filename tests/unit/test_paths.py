from pathlib import Path

from fair_ros.utils import paths


def test_defaults(monkeypatch):
    monkeypatch.delenv("FAIR_ROS_VAR_DIR", raising=False)
    monkeypatch.delenv("FAIR_ROS_CONFIG_DIR", raising=False)
    assert paths.var_dir() == Path("/var/fair-ros")
    assert paths.robot_identity_path() == Path("/etc/fair-ros/robot_identity.yaml")
    assert paths.bags_dir() == Path("/var/fair-ros/spool/bags")
    assert paths.index_db_path() == Path("/var/fair-ros/index.db")
    assert paths.watchdog_state_path() == Path("/var/fair-ros/watchdog.state")


def test_env_override(fair_dirs):
    assert paths.var_dir() == fair_dirs["var"]
    assert paths.spool_dir() == fair_dirs["var"] / "spool"
    assert paths.harvest_json_path() == fair_dirs["var"] / "spool" / "harvest.json"
    assert paths.mission_context_path() == \
        fair_dirs["var"] / "spool" / "mission_context.json"
    assert paths.staging_dir() == fair_dirs["var"] / "archive" / ".staging"
    assert paths.robot_identity_path().parent == fair_dirs["cfg"]

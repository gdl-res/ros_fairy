<p align="center">
  <img src="ros_fairy_logo.jpg" alt="ROS F.A.I.R.y" width="420">
</p>

<p align="center">
  <strong>Make ROS 2 field mission data FAIR-compliant with zero friction:</strong><br>
  automatic context capture, plain-language briefings, RO-Crate archives.
</p>

---

## What it is

`fair_ros` is a ROS 2 CLI extension (`ros2 fairy ...`) plus a background watchdog
service. An operator answers five questions before a run; everything else —
robot identity, ROS graph and node descriptions, sensors seen publishing,
Python environment, Docker images, hardware devices, system info — is harvested
automatically and written alongside the bags as an
[RO-Crate](https://www.researchobject.org/ro-crate/) archive that can be
verified, diffed, and shared as a single checksummed file.

## Install

Build it into a ROS 2 workspace (`ament_python`, ROS 2 Humble/Jazzy or newer):

```bash
cd ~/ros2_ws/src && git clone https://github.com/gdl-res/ros_fairy.git
cd ~/ros2_ws && colcon build --packages-select fair_ros
source install/setup.bash
sudo ros2 fairy setup          # identity file, directories, watchdog service
ros2 fairy doctor              # confirm the robot is ready to capture
```

## A mission, start to finish

```bash
ros2 fairy mission_start       # five questions describing the run
ros2 fairy mission_record      # wraps `ros2 bag record` with safety checks
ros2 fairy mission_close       # review the briefing, then save or discard
ros2 fairy list                # missions saved on this robot
ros2 fairy export 1            # bundle the newest mission + sha256 sidecar
```

## Commands

| Verb | What it does |
| --- | --- |
| `setup` | One-time robot setup: identity file, directories, watchdog service |
| `doctor` | Check that this robot is ready to capture a mission |
| `mission_start` | Answer five quick questions to describe the mission |
| `mission_record` | Record mission data (wraps `ros2 bag record`) |
| `mission_status` | Show what the recording assistant is doing right now |
| `mission_close` | Review the finished mission and save it or discard it |
| `list` | List the missions saved on this robot |
| `diff` | Compare two missions and show what changed |
| `verify` | Check that a saved archive is complete and unmodified |
| `export` | Package a saved mission into one portable file |
| `repair` | Make unplayable (bad-clock) recordings playable |
| `adopt` | Ingest a bag recorded outside `mission_record` |
| `reindex` | Rebuild the mission list from the archives on disk |

## Where things live

| Path | Contents |
| --- | --- |
| `/etc/fair-ros` | `robot_identity.yaml`, watchdog environment |
| `/var/fair-ros/spool` | live harvest, session env, in-progress bags |
| `/var/fair-ros/archive` | saved mission crates and the mission index |

Both roots are overridable with `FAIR_ROS_CONFIG_DIR` and `FAIR_ROS_VAR_DIR`.

## Development

```bash
pip install -e '.[dev]'
pytest                        # unit + integration, no ROS required
pytest -m ros                 # live smoke tests, on a sourced ROS 2 box
ruff check . && mypy fair_ros
```

## License

Apache-2.0 — see [LICENSE](LICENSE).

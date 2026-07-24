#!/usr/bin/env bash
# Record tiny REAL rosbag2 fixtures (sqlite3 + mcap) inside a ros:jazzy
# container, so tests/integration/test_real_bags.py can validate the parser
# and storage readers against genuine Jazzy (metadata version 9) output.
#
# Usage:  sudo tools/record_real_fixtures.sh [output_dir]
#   output_dir defaults to tests/fixtures/ next to this repo.
#
# Produces:  <output_dir>/real_sqlite3/  and  <output_dir>/real_mcap/
# Each is a few seconds of /chatter (~1 Hz) -> well under 100 KB, commit-safe.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$REPO/tests/fixtures}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"

# Who should own the resulting files (this script runs under sudo).
OWNER_UID="${SUDO_UID:-$(id -u)}"
OWNER_GID="${SUDO_GID:-$(id -g)}"

echo ">> Recording real bags into: $OUT"

docker run --rm -v "$OUT:/out" ros:jazzy bash -c '
set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  ros-jazzy-demo-nodes-cpp \
  ros-jazzy-rosbag2 \
  ros-jazzy-rosbag2-storage-mcap >/dev/null
source /opt/ros/jazzy/setup.bash
cd /tmp

ros2 run demo_nodes_cpp talker >/dev/null 2>&1 &
TALKER=$!
sleep 2

# timeout -s INT mimics a clean Ctrl-C so rosbag2 finalises metadata.yaml.
timeout -s INT 6 ros2 bag record -s sqlite3 -o real_sqlite3 /chatter || true
sleep 1
timeout -s INT 6 ros2 bag record -s mcap    -o real_mcap    /chatter || true

kill "$TALKER" 2>/dev/null || true

rm -rf /out/real_sqlite3 /out/real_mcap
cp -r real_sqlite3 /out/
cp -r real_mcap /out/

echo "---- metadata.yaml (mcap) ----"
cat /out/real_mcap/metadata.yaml
echo "---- file sizes ----"
du -ab /out/real_sqlite3 /out/real_mcap
'

# Hand ownership back to the invoking user so they are not root-owned.
chown -R "$OWNER_UID:$OWNER_GID" "$OUT/real_sqlite3" "$OUT/real_mcap"

echo ">> Done. Fixtures written to $OUT/real_sqlite3 and $OUT/real_mcap"

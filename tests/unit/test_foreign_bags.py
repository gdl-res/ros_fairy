"""Foreign-bag detection: /proc recorder scan, watchdog adoption, `adopt`,
assembler copy-not-move, and the vanished-source warning."""

import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from inotify_simple import flags

from fair_ros.archive import assembler
from fair_ros.manifest import builder
from fair_ros.subcommands import adopt
from fair_ros.utils import fsio, paths
from fair_ros.watchdog import recorder_scan
from fair_ros.watchdog import watchdog as wd_mod
from fair_ros.watchdog.watchdog import IDLE, RECORDING, Watchdog
from tests.conftest import make_bag
from tests.unit.test_watchdog import (
    T0,
    FakeClock,
    FakeINotify,
    _steady,
    good_pipeline,
)

SCAN_S = wd_mod.FOREIGN_SCAN_INTERVAL_S


# -- recorder_scan parsing helpers (pure) --------------------------------------

def test_is_record_cmd_matches_only_recorder():
    assert recorder_scan._is_record_cmd(["ros2", "bag", "record", "-o", "x"])
    assert recorder_scan._is_record_cmd(
        ["python3", "/opt/ros/jazzy/bin/ros2", "bag", "record", "--all"])
    assert not recorder_scan._is_record_cmd(["ros2", "bag", "play", "x"])
    assert not recorder_scan._is_record_cmd(["ros2", "bag", "info", "x"])
    assert not recorder_scan._is_record_cmd(["ros2", "bag", "convert", "x"])
    assert not recorder_scan._is_record_cmd(["ros2", "topic", "list"])
    assert not recorder_scan._is_record_cmd([])
    # an output dir or topic named like another verb must not be excluded
    assert recorder_scan._is_record_cmd(
        ["ros2", "bag", "record", "-o", "info", "/chatter"])


def test_output_arg_forms():
    assert recorder_scan._output_arg(["record", "-o", "run"]) == "run"
    assert recorder_scan._output_arg(["record", "--output", "run"]) == "run"
    assert recorder_scan._output_arg(["record", "--output=run"]) == "run"
    assert recorder_scan._output_arg(["record", "-o=run"]) == "run"
    assert recorder_scan._output_arg(["record", "--all"]) is None


def test_resolve_output_explicit(tmp_path):
    active = make_bag(tmp_path / "run", {"/t": [1.0, 2.0]})
    (active / "metadata.yaml").unlink()  # storage present, no metadata = live
    argv = ["ros2", "bag", "record", "-o", str(active), "/t"]
    assert recorder_scan._resolve_output(argv, tmp_path) == active

    finished = make_bag(tmp_path / "done", {"/t": [1.0]})  # has metadata
    argv2 = ["ros2", "bag", "record", "-o", "done", "/t"]
    assert recorder_scan._resolve_output(argv2, tmp_path) is None
    assert finished.is_dir()


def test_resolve_output_default_name(tmp_path):
    bag = make_bag(tmp_path / "rosbag2_2026_06_24-10_00_00", {"/t": [1.0]})
    (bag / "metadata.yaml").unlink()
    assert recorder_scan._resolve_output(["ros2", "bag", "record"], tmp_path) == bag


def test_scan_returns_empty_when_no_recorder():
    assert recorder_scan.scan() == []  # no ros2 bag record on this machine


# -- containerised recorders (mount-namespace translation) ---------------------

def test_parse_mountinfo_unescapes_octal():
    entries = recorder_scan._parse_mountinfo(
        "31 1 259:2 / /mnt/usb\\040stick rw,relatime - ext4 /dev/sda1 rw\n"
        "garbage line\n")
    assert entries == [(Path("/mnt/usb stick"), "259:2", Path("/"))]


def test_host_path_translates_bind_mount():
    # A container bind-mounts /home/jo/bags at /home/ros/bags (the jo-zotac
    # setup); the host has the whole filesystem mounted at /.
    pid_mi = ("650 630 259:2 /home/jo/bags /home/ros/bags rw - ext4 /dev/n rw\n"
              "649 630 0:79 / / rw - overlay overlay rw\n")
    self_mi = "31 1 259:2 / / rw - ext4 /dev/n rw\n"
    assert recorder_scan._host_path(
        Path("/home/ros/bags/run1"), pid_mi, self_mi) == \
        Path("/home/jo/bags/run1")
    # the mount point itself translates too
    assert recorder_scan._host_path(
        Path("/home/ros/bags"), pid_mi, self_mi) == Path("/home/jo/bags")


def test_host_path_prefers_longest_prefix():
    pid_mi = ("649 630 0:79 / / rw - overlay overlay rw\n"
              "650 630 259:2 /a /data rw - ext4 /dev/n rw\n"
              "651 650 259:3 /b /data/inner rw - ext4 /dev/m rw\n")
    self_mi = ("31 1 259:2 / / rw - ext4 /dev/n rw\n"
               "32 1 259:3 / /mnt/disk2 rw - ext4 /dev/m rw\n")
    assert recorder_scan._host_path(
        Path("/data/inner/run"), pid_mi, self_mi) == \
        Path("/mnt/disk2/b/run")


def test_host_path_none_for_container_private_path():
    pid_mi = "649 630 0:79 / / rw - overlay overlay rw\n"
    self_mi = "31 1 259:2 / / rw - ext4 /dev/n rw\n"  # no 0:79 mount on host
    assert recorder_scan._host_path(
        Path("/root/data/run"), pid_mi, self_mi) is None


def _fake_proc(tmp_path):
    """A fabricated /proc with self plus room for recorder pids."""
    proc = tmp_path / "proc"
    (proc / "self" / "ns").mkdir(parents=True)
    (proc / "self" / "ns" / "mnt").write_text("host-ns")
    return proc


def _fake_recorder(proc, pid, argv, *, root=None, cwd="/", environ="",
                   mountinfo="", same_ns=True):
    d = proc / str(pid)
    (d / "ns").mkdir(parents=True)
    (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
    (d / "environ").write_bytes(environ.encode())
    (d / "mountinfo").write_text(mountinfo)
    os.symlink(cwd, d / "cwd")
    os.symlink(root if root is not None else "/", d / "root")
    if same_ns:  # same inode as self's ns/mnt = same mount namespace
        os.link(proc / "self" / "ns" / "mnt", d / "ns" / "mnt")
    else:
        (d / "ns" / "mnt").write_text(f"ns-{pid}")


def _patched_scan(monkeypatch, proc, self_mountinfo=""):
    (proc / "self" / "mountinfo").write_text(self_mountinfo)
    monkeypatch.setattr(recorder_scan, "PROC", proc)
    monkeypatch.setattr(recorder_scan, "_reported", set())
    return recorder_scan.scan()


def test_scan_detects_host_recorder_in_fake_proc(tmp_path, monkeypatch):
    bag = make_bag(tmp_path / "hostrun", {"/t": [1.0]})
    (bag / "metadata.yaml").unlink()
    proc = _fake_proc(tmp_path)
    _fake_recorder(proc, 100,
                   ["ros2", "bag", "record", "-o", str(bag), "/t"],
                   cwd=str(tmp_path), environ="ROS_DOMAIN_ID=7\0X=1\0")
    found = _patched_scan(monkeypatch, proc)
    assert found == [{"pid": 100, "output_dir": bag.resolve(),
                      "discovery": {"ROS_DOMAIN_ID": "7"}}]


def test_scan_translates_containerised_recorder(tmp_path, monkeypatch):
    # Host side of the bind mount, where the bag really lives.
    host_bags = tmp_path / "hostbags"
    bag = make_bag(host_bags / "run1", {"/t": [1.0]})
    (bag / "metadata.yaml").unlink()
    # The container's rootfs: /home/ros/bags is the bind mount (a symlink
    # stands in for it so the /proc/<pid>/root portal reaches the same dir).
    croot = tmp_path / "croot"
    (croot / "home" / "ros").mkdir(parents=True)
    os.symlink(host_bags, croot / "home" / "ros" / "bags")
    proc = _fake_proc(tmp_path)
    _fake_recorder(
        proc, 200,
        ["ros2", "bag", "record", "-o", "/home/ros/bags/run1", "/t"],
        root=str(croot), cwd="/home/ros", same_ns=False,
        environ="RMW_IMPLEMENTATION=rmw_cyclonedds_cpp\0",
        mountinfo=("650 630 259:9 /jo/bags /home/ros/bags rw - ext4 /dev/n rw\n"
                   "649 630 0:79 / / rw - overlay overlay rw\n"))
    found = _patched_scan(
        monkeypatch, proc,
        self_mountinfo=f"31 1 259:9 /jo/bags {host_bags} rw - ext4 /dev/n rw\n")
    assert found == [{"pid": 200, "output_dir": bag.resolve(),
                      "discovery": {"RMW_IMPLEMENTATION": "rmw_cyclonedds_cpp"}}]


def test_scan_skips_container_private_path_with_warning(tmp_path, monkeypatch):
    croot = tmp_path / "croot"
    bag = make_bag(croot / "root" / "data" / "run2", {"/t": [1.0]})
    (bag / "metadata.yaml").unlink()
    proc = _fake_proc(tmp_path)
    _fake_recorder(
        proc, 300,
        ["ros2", "bag", "record", "-o", "/root/data/run2", "/t"],
        root=str(croot), cwd="/root", same_ns=False,
        mountinfo="649 630 0:79 / / rw - overlay overlay rw\n")
    with mock.patch.object(recorder_scan, "log") as fake_log:
        assert _patched_scan(monkeypatch, proc) == []
        assert recorder_scan.scan() == []  # second pass: still skipped...
    warned = [c for c in fake_log.log.call_args_list
              if "inside a container" in c.args[1]]
    assert len(warned) == 1  # ...but warned only once
    assert warned[0].args[0] == recorder_scan.logging.WARNING


# -- watchdog foreign detection ------------------------------------------------

def _foreign_dog(found):
    ino, clock = FakeINotify(), FakeClock()
    dog = Watchdog(inotify=ino, clock=clock, pipeline=good_pipeline,
                   harvest_in_thread=False, scan_recorders=lambda: found)
    dog.start()
    return ino, clock, dog


def test_foreign_recording_detected_and_finalised(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext_run", {"/fix": _steady(T0, T0 + 60, 10)})
    found = [{"pid": os.getpid(), "output_dir": foreign, "discovery": {}}]
    ino, clock, dog = _foreign_dog(found)

    clock.now += SCAN_S  # let the poller fire
    dog.step(0)
    assert dog.state == RECORDING
    assert dog.active_bag_dir == foreign
    assert foreign in dog._foreign

    ino.emit(foreign, flags.CLOSE_WRITE, "metadata.yaml")
    dog.step(0)
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert [b["source"] for b in harvest["bags"]] == ["detected"]
    assert harvest["bags"][0]["path"] == str(foreign)
    assert foreign.is_dir()  # referenced in place, not moved


def test_foreign_recorder_exit_finalises(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext2", {"/fix": _steady(T0, T0 + 60, 10)})
    dead_pid = 0x7FFFFFFF  # no such process
    found = [{"pid": dead_pid, "output_dir": foreign, "discovery": {}}]
    _, clock, dog = _foreign_dog(found)

    clock.now += SCAN_S
    dog.step(0)  # poller adopts, then the recorder-exit hint finalises it
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert harvest["bags"][0]["source"] == "detected"


def test_foreign_harvest_adopts_recorder_environ(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext3", {"/fix": _steady(T0, T0 + 5, 10)})
    found = [{"pid": os.getpid(), "output_dir": foreign,
              "discovery": {"ROS_DOMAIN_ID": "42"}}]
    ino, clock, dog = _foreign_dog(found)
    with mock.patch.dict(wd_mod.os.environ, {"ROS_DOMAIN_ID": "0"}, clear=False):
        clock.now += SCAN_S
        dog.step(0)
        assert dog.state == RECORDING
        # the harvest ran on the recorder's partition, not the watchdog default
        assert wd_mod.os.environ["ROS_DOMAIN_ID"] == "42"


def test_quiet_foreign_bag_waits_for_recorder_exit(fair_dirs, tmp_path):
    # No bag-file activity for BAG_INACTIVITY_S, but the recorder still runs
    # (quiet topics, buffered writes): must NOT finalise until it exits.
    foreign = make_bag(tmp_path / "ext5", {"/fix": _steady(T0, T0 + 60, 10)})
    metadata = (foreign / "metadata.yaml").read_text()
    (foreign / "metadata.yaml").unlink()
    found = [{"pid": os.getpid(), "output_dir": foreign, "discovery": {}}]
    ino, clock, dog = _foreign_dog(found)

    clock.now += SCAN_S
    dog.step(0)
    assert dog.state == RECORDING

    clock.now += wd_mod.BAG_INACTIVITY_S + 5
    dog.step(0)
    assert dog.state == RECORDING  # alive recorder: quiet is not finished

    # The recorder exits and metadata lands: the exit hint finalises it with
    # real bag metadata, not the crash fallback.
    (foreign / "metadata.yaml").write_text(metadata)
    dog._foreign[foreign]["pid"] = 0x7FFFFFFF  # no such process
    clock.now += 1
    dog.step(0)
    assert dog.state == IDLE
    harvest, _ = builder.load_spool()
    assert harvest["bags"][0]["source"] == "detected"
    assert harvest["bags"][0]["storage_format"] != "unknown"


def test_foreign_recording_queued_while_busy(fair_dirs, tmp_path):
    foreign = make_bag(tmp_path / "ext4", {"/fix": _steady(T0, T0 + 60, 10)})
    found = [{"pid": os.getpid(), "output_dir": foreign, "discovery": {}}]
    ino, clock, dog = _foreign_dog(found)

    # A spool recording is already in progress.
    bag_a = make_bag(paths.bags_dir() / "rosbag2_a",
                     {"/fix": _steady(T0, T0 + 60, 10)})
    ino.emit_dir_created(paths.bags_dir(), "rosbag2_a")
    dog.step(0)
    ino.emit_file(bag_a, "rosbag2_a_0.db3")
    dog.step(0)
    assert dog.active_bag_dir == bag_a

    clock.now += SCAN_S
    dog.step(0)
    assert dog.active_bag_dir == bag_a       # not pre-empted
    assert foreign in dog.queued_bags        # one bag, one mission


def test_poller_ignores_spool_bags(fair_dirs):
    dog = Watchdog(inotify=FakeINotify(), pipeline=good_pipeline,
                   harvest_in_thread=False, scan_recorders=lambda: [])
    spool_bag = (paths.bags_dir() / "rosbag2_x").resolve()
    assert dog._is_tracked(spool_bag)               # inotify already covers it
    assert not dog._is_tracked((paths.archive_dir() / "elsewhere").resolve())


# -- ros2 fairy adopt -----------------------------------------------------------

def _seed_harvest():
    fsio.atomic_write_json(paths.harvest_json_path(), good_pipeline())


def _args(bagdir, json=False):
    return SimpleNamespace(bagdir=str(bagdir), json=json, debug=False)


def test_adopt_appends_bag(fair_dirs, tmp_path):
    _seed_harvest()
    bag = make_bag(tmp_path / "adopt_me", {"/fix": _steady(T0, T0 + 30, 10)})
    assert adopt.run(_args(bag)) == 0
    harvest, _ = builder.load_spool()
    assert harvest["bags"][-1]["source"] == "adopted"
    assert harvest["bags"][-1]["path"] == str(bag.resolve())


def test_adopt_rejects_non_bag(fair_dirs, tmp_path):
    assert adopt.run(_args(tmp_path / "nope")) == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert adopt.run(_args(empty)) == 1  # a dir, but not a recording


def test_adopt_refuses_while_recording(fair_dirs, tmp_path):
    fsio.atomic_write_json(paths.watchdog_state_path(),
                           {"version": 1, "state": "RECORDING"})
    bag = make_bag(tmp_path / "busy", {"/fix": [T0, T0 + 1]})
    assert adopt.run(_args(bag)) == 1


def test_adopt_is_idempotent(fair_dirs, tmp_path):
    _seed_harvest()
    bag = make_bag(tmp_path / "twice", {"/fix": [T0, T0 + 1]})
    assert adopt.run(_args(bag)) == 0
    assert adopt.run(_args(bag)) == 0
    harvest, _ = builder.load_spool()
    assert sum(1 for b in harvest["bags"]
               if b["path"] == str(bag.resolve())) == 1


def test_adopt_harvests_when_no_context(fair_dirs, tmp_path, monkeypatch):
    monkeypatch.setattr(adopt.watchdog, "run_pipeline", good_pipeline)
    bag = make_bag(tmp_path / "cold", {"/fix": [T0, T0 + 1]})
    assert adopt.run(_args(bag)) == 0
    harvest, _ = builder.load_spool()
    assert harvest is not None
    assert harvest["bags"][-1]["source"] == "adopted"


# -- assembler: copy foreign, drop vanished ------------------------------------

def _bag_entry(bag_path, source):
    return {
        "path": str(bag_path), "source": source, "storage_format": "sqlite3",
        "size_bytes": fsio.dir_size_bytes(bag_path), "start_time": None,
        "end_time": None, "duration_s": None, "message_count": 0,
        "topics": [], "health_warnings": [],
    }


def _record_with_bags(harvest, *bags):
    harvest["bags"] = list(bags)
    context = builder.new_mission_context("Op", "Goal", "Loc")
    return builder.build(harvest, context), harvest


def _record_with_bag(bag_path, source):
    return _record_with_bags(good_pipeline(),
                             _bag_entry(bag_path, source))


def test_foreign_bag_copied_not_moved(fair_dirs, tmp_path):
    bag = make_bag(tmp_path / "ext", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bag(bag, "detected")
    crate = assembler.assemble(record, harvest)
    assert (crate / "bags" / "ext" / "metadata.yaml").is_file()
    assert bag.is_dir()  # original left in place


def test_vanished_foreign_bag_skipped(fair_dirs, tmp_path):
    bag = make_bag(tmp_path / "gone", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bag(bag, "detected")
    shutil.rmtree(bag)  # operator moved/deleted it before saving
    crate = assembler.assemble(record, harvest)
    assert not (crate / "bags" / "gone").exists()
    assert record.bags == []


def test_spool_bag_still_moved(fair_dirs):
    bag = make_bag(paths.bags_dir() / "rosbag2_m", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bag(bag, "mission_record")
    crate = assembler.assemble(record, harvest)
    assert (crate / "bags" / "rosbag2_m").is_dir()
    assert not bag.exists()  # moved out of the spool


def test_foreign_bag_vanishing_mid_assembly_is_not_fatal(
        fair_dirs, tmp_path, monkeypatch):
    """A foreign bag deleted *during* assembly is dropped, not fatal (#35)."""
    foreign = make_bag(tmp_path / "ext", {"/fix": [T0, T0 + 1]})
    spool = make_bag(paths.bags_dir() / "rosbag2_keep", {"/fix": [T0, T0 + 1]})
    record, harvest = _record_with_bags(
        good_pipeline(),
        _bag_entry(foreign, "detected"),
        _bag_entry(spool, "mission_record"))

    def vanishing_copytree(src, dst, *a, **k):
        shutil.rmtree(src)  # operator deletes it just as the copy begins
        raise FileNotFoundError(src)
    monkeypatch.setattr(assembler.shutil, "copytree", vanishing_copytree)

    crate = assembler.assemble(record, harvest)
    # the save completed; the spool bag survived and the foreign one was dropped
    assert (crate / "bags" / "rosbag2_keep").is_dir()
    assert not (crate / "bags" / "ext").exists()
    assert [b.source for b in record.bags] == ["mission_record"]
    # and the crate is internally consistent (manifest matches what's on disk)
    assert all((crate / b.path).is_dir() for b in record.bags)


# -- builder: vanished-foreign warning -----------------------------------------

def _harvest_with_foreign(path):
    harvest = good_pipeline()
    harvest["bags"] = [{"path": str(path), "source": "detected"}]
    return harvest


def test_warns_on_vanished_foreign(fair_dirs):
    warns = builder.harvest_level_warnings(
        _harvest_with_foreign("/no/such/run"))
    assert any("can no longer be found" in w for w in warns)


def test_no_warning_for_present_foreign(fair_dirs, tmp_path):
    bag = make_bag(tmp_path / "here", {"/fix": [T0]})
    warns = builder.harvest_level_warnings(_harvest_with_foreign(bag))
    assert not any("can no longer be found" in w for w in warns)

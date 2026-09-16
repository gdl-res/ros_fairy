"""ros2 fairy export — package a saved mission as one portable, checksummed file.

A mission archive is an RO-Crate *directory*; sharing it means an operator
hand-zips it (and hopes the transfer didn't corrupt anything). This makes that a
first-class, safe step: it bundles the crate into a single ``.zip`` (or ``.tar``)
with a top-level folder, writes a ``sha256sum``-compatible sidecar so the
recipient can prove the transfer, and refuses to clobber an existing file. The
crate's own per-file checksums still let the recipient run ``ros2 fairy verify``
after unpacking.

Read-only with respect to the archive and index.
"""

import json
import logging
import os
import shutil
import tarfile
import zipfile
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
)
from rich.prompt import Confirm

from ros_fairy.archive import index, locate
from ros_fairy.subcommands import VerbExtension, _configure_logging, guarded_main
from ros_fairy.ui.review import human_size
from ros_fairy.utils import fsio

log = logging.getLogger("ros_fairy.subcommands.export")

FORMATS = ("zip", "tar")
_EXT = {"zip": ".zip", "tar": ".tar"}


def _resolve_output(crate: Path, output: str | None, fmt: str) -> Path:
    """Where to write the bundle, from --output (file or dir) and format."""
    default_name = crate.name + _EXT[fmt]
    if not output:
        return Path.cwd() / default_name
    out = Path(output).expanduser()
    if out.is_dir() or output.endswith(os.sep):
        return out / default_name
    return out


def _bundle_files(crate: Path) -> list[tuple[Path, str]]:
    """(absolute path, arcname) for every file, under a top-level crate folder."""
    return [(f, f"{crate.name}/{f.relative_to(crate).as_posix()}")
            for f in sorted(crate.rglob("*")) if f.is_file()]


def _write_bundle(files: list[tuple[Path, str]], dest: Path, fmt: str,
                  console: Console) -> None:
    """Pack files into dest atomically (.part then rename). ZIP/TAR are stored
    uncompressed — bag data (MCAP, images) is already compressed, so deflating
    multi-GB recordings only burns CPU for no gain."""
    total = sum(f.stat().st_size for f, _ in files) or 1
    part = dest.with_name(dest.name + ".part")
    progress = Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(), DownloadColumn(), TimeRemainingColumn(),
        console=console, transient=True)
    try:
        with progress:
            task = progress.add_task("Packaging", total=total)
            if fmt == "zip":
                with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED,
                                     allowZip64=True) as zf:
                    for src, arc in files:
                        zf.write(src, arc)
                        progress.advance(task, src.stat().st_size)
            else:
                with tarfile.open(part, "w") as tf:
                    for src, arc in files:
                        tf.add(str(src), arcname=arc, recursive=False)
                        progress.advance(task, src.stat().st_size)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def _local_date(iso: str):
    try:
        return datetime.fromisoformat(iso).astimezone().date()
    except ValueError:
        return None


def _export_one(crate: Path, output_arg: str | None, fmt: str, force: bool,
                console: Console) -> dict:
    """Export one archive. Never raises — failures come back as {"ok": False,
    "error": ...} so both the single-mission and batch paths can report them
    without a bundle failure aborting the whole batch."""
    try:
        record = locate.load_record(crate)
    except locate.LocateError as exc:
        return {"mission_id": None, "source": str(crate), "ok": False,
                "error": str(exc)}

    dest = _resolve_output(crate, output_arg, fmt)
    if dest.exists() and not force:
        return {"mission_id": record.identity.mission_id, "source": str(crate),
                "ok": False,
                "error": f"{dest} already exists (use --force to overwrite)"}

    # Integrity gate: warn loudly if the source archive doesn't verify, but let
    # the operator export it anyway (they asked, and a flawed copy can still be
    # worth shipping for diagnosis). verify must never block the export, so a
    # broken index or any other hiccup degrades to "unknown".
    try:
        from ros_fairy.subcommands.verify import _overall, verify_archive
        verify_result = _overall(verify_archive(crate))
    except Exception:
        verify_result = "unknown"
    if verify_result == "fail":
        console.print(f"[yellow]Warning: {record.identity.mission_id} failed "
                      "integrity checks (run `ros2 fairy verify` for "
                      "details). Exporting anyway.[/yellow]")

    files = _bundle_files(crate)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_bundle(files, dest, fmt, console)
    except OSError as exc:
        return {"mission_id": record.identity.mission_id, "source": str(crate),
                "ok": False,
                "error": f"couldn't write the bundle: {exc.strerror or exc}"}

    digest = fsio.sha256_file(dest)
    checksum_path = dest.with_name(dest.name + ".sha256")
    # sha256sum-compatible: recipient runs `sha256sum -c <file>.sha256`.
    fsio.atomic_write_text(checksum_path, f"{digest}  {dest.name}\n")

    # Only reached once the bundle file actually exists on disk — this is
    # what `export --all` treats as "already exported".
    try:
        index.mark_exported(record.identity.mission_id, dest, fmt, digest)
    except Exception:
        log.warning("couldn't record export of %s in the index",
                   record.identity.mission_id, exc_info=True)

    return {
        "mission_id": record.identity.mission_id,
        "source": str(crate),
        "ok": True,
        "bundle": str(dest),
        "format": fmt,
        "size_bytes": dest.stat().st_size,
        "sha256": digest,
        "checksum_file": str(checksum_path),
        "verify_result": verify_result,
    }


def _print_result(result: dict, console: Console) -> None:
    console.print(
        f"[green]Exported[/green] {result['mission_id']} → "
        f"{result['bundle']} [dim]({human_size(result['size_bytes'])})[/dim]")
    console.print(f"[dim]sha256: {result['sha256']}[/dim]")
    console.print(f"[dim]checksum saved to "
                  f"{Path(result['checksum_file']).name} — the recipient can "
                  "run `ros2 fairy verify` after unpacking.[/dim]")


def _run_batch(args, console: Console, candidates: list[dict],
              what: str) -> int:
    """Shared body of --all/--today: confirm, check disk space, export each."""
    exclude = set(getattr(args, "exclude", None) or [])
    candidates = [r for r in candidates if r["mission_id"] not in exclude]
    candidates.sort(key=lambda r: r["created_at"])

    if not candidates:
        if getattr(args, "json", False):
            print(json.dumps({"exported": 0, "failed": 0, "results": []},
                             indent=2))
        else:
            console.print("Nothing to export.")
        return 0

    fmt = getattr(args, "format", "zip")
    output_arg = getattr(args, "output", None)
    out_dir = Path(output_arg).expanduser() if output_arg else Path.cwd()
    if out_dir.exists() and not out_dir.is_dir():
        console.print(f"[red]{out_dir} is not a directory — --output must be "
                      f"a directory when exporting {what}.[/red]")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)

    total_bytes = sum(r["size_bytes"] for r in candidates)
    console.print(f"{len(candidates)} mission(s) to export {what}, "
                  f"~{human_size(total_bytes)} total.")

    free = shutil.disk_usage(out_dir).free
    if free < total_bytes:
        proceed = Confirm.ask(
            f"Only {human_size(free)} free at {out_dir}, but this needs "
            f"about {human_size(total_bytes)}. Continue anyway?",
            default=False, console=console)
        if not proceed:
            return 1

    proceed = Confirm.ask(
        f"This will export {len(candidates)} mission(s) and could take a "
        "while. Continue?", default=True, console=console)
    if not proceed:
        return 0

    force = getattr(args, "force", False)
    results = []
    for row in candidates:
        result = _export_one(Path(row["archive_path"]), output_arg, fmt,
                             force, console)
        results.append(result)
        if not getattr(args, "json", False):
            if result["ok"]:
                console.print(f"[green]Exported[/green] {result['mission_id']}"
                              f" → {result['bundle']}")
            else:
                console.print(f"[red]Failed[/red] {row['mission_id']}: "
                              f"{result['error']}")

    ok_count = sum(1 for r in results if r["ok"])
    if getattr(args, "json", False):
        print(json.dumps(
            {"exported": ok_count, "failed": len(results) - ok_count,
             "results": results}, indent=2))
    else:
        console.print(f"Done: {ok_count} exported, "
                      f"{len(results) - ok_count} failed.")
    return 0 if ok_count == len(results) else 1


def run(args, console: Console | None = None) -> int:
    _configure_logging(getattr(args, "debug", False))
    console = console or Console()

    all_mode = getattr(args, "all", False)
    today_mode = getattr(args, "today", False)
    mission_arg = getattr(args, "mission", None)

    if all_mode and today_mode:
        console.print("[red]--all and --today can't be used together.[/red]")
        return 1
    if (all_mode or today_mode) and mission_arg:
        console.print("[red]Give a mission, or --all/--today — not "
                      "both.[/red]")
        return 1

    if all_mode or today_mode:
        try:
            rows, _total = index.query(limit=10_000)
        except index.IndexUnavailableError as exc:
            console.print(f"[red]{exc}[/red]")
            return 1
        if all_mode:
            already = index.exported_mission_ids()
            candidates = [r for r in rows if r["mission_id"] not in already]
            what = "not already exported"
        else:
            today = datetime.now().astimezone().date()
            candidates = [r for r in rows
                         if _local_date(r["created_at"]) == today]
            what = "from today"
        return _run_batch(args, console, candidates, what)

    fmt = getattr(args, "format", "zip")
    try:
        crate = locate.resolve_archive(mission_arg or "1")
    except locate.LocateError as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    result = _export_one(crate, getattr(args, "output", None), fmt,
                         getattr(args, "force", False), console)
    if not result["ok"]:
        console.print(f"[red]{result['error']}[/red]")
        return 1

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        _print_result(result, console)
    return 0


class ExportVerb(VerbExtension):
    """Package a saved mission into one portable file for sharing."""

    def add_arguments(self, parser, cli_name):
        parser.add_argument(
            "mission", nargs="?",
            help="mission to export: number (1 = newest), archive path, or "
                 "mission ID. Defaults to the most recent mission. Not used "
                 "with --all/--today.")
        parser.add_argument(
            "--all", action="store_true",
            help="export every saved mission that hasn't been successfully "
                 "exported yet")
        parser.add_argument(
            "--today", action="store_true",
            help="export every mission recorded today")
        parser.add_argument(
            "--exclude", "-e", action="append", metavar="MISSION_ID",
            help="mission ID to skip; repeatable. Only applies with "
                 "--all/--today")
        parser.add_argument(
            "--output", "-o",
            help="output file (single mission), or a directory to write "
                 "<mission>.<ext> into — required to be a directory when "
                 "exporting more than one mission (default: current "
                 "directory)")
        parser.add_argument(
            "--format", choices=FORMATS, default="zip",
            help="bundle format (default: zip)")
        parser.add_argument(
            "--force", action="store_true",
            help="overwrite output files that already exist")
        parser.add_argument(
            "--json", action="store_true",
            help="machine-readable output for scripts")
        parser.add_argument(
            "--debug", action="store_true",
            help="verbose logging to stderr (for engineers)")

    def main(self, *, args):
        return guarded_main(run, args)

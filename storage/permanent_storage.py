#!/usr/bin/env python3
"""
Permanent Storage Manager

A safety-first, mouse-aware terminal UI for configuring external Linux storage
as a persistent UUID-based mount. The interface uses the terminal's native
background instead of dialog/whiptail's blue screen.

Supported workflows:
  * Persistently mount an existing filesystem without formatting it.
  * Repartition a selected non-system disk as GPT + one ext4 filesystem.

This tool edits /etc/fstab. It does NOT perform a firmware secure erase.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

APP_NAME = "Permanent Storage Manager"
VERSION = "2.5.0"
BUILD_ID = "busy-device-preflight-20260723-01"
FSTAB = Path("/etc/fstab")
LOG_FILE = Path("/var/log/permanent-storage-manager.log")
INSTALL_PATH = Path("/usr/local/sbin/permanent-storage-manager")
CRITICAL_MOUNTS = ("/", "/boot", "/boot/efi", "/usr", "/var")

# Utilities required for every supported workflow. systemctl/systemd-escape and
# udevadm are optional because the program also supports non-systemd systems.
REQUIRED_COMMANDS = {
    "lsblk",
    "blkid",
    "findmnt",
    "mount",
    "umount",
    "wipefs",
    "parted",
    "partprobe",
    "mkfs.ext4",
}

PACKAGE_MAP = {
    "apt": {
        "python": "python3",
        "curses": "python3",
        "lsblk": "util-linux",
        "blkid": "util-linux",
        "findmnt": "util-linux",
        "mount": "util-linux",
        "umount": "util-linux",
        "wipefs": "util-linux",
        "parted": "parted",
        "partprobe": "parted",
        "mkfs.ext4": "e2fsprogs",
    },
    "dnf": {
        "python": "python3",
        "curses": "python3",
        "lsblk": "util-linux",
        "blkid": "util-linux",
        "findmnt": "util-linux",
        "mount": "util-linux",
        "umount": "util-linux",
        "wipefs": "util-linux",
        "parted": "parted",
        "partprobe": "parted",
        "mkfs.ext4": "e2fsprogs",
    },
    "yum": {
        "python": "python3",
        "curses": "python3",
        "lsblk": "util-linux",
        "blkid": "util-linux",
        "findmnt": "util-linux",
        "mount": "util-linux",
        "umount": "util-linux",
        "wipefs": "util-linux",
        "parted": "parted",
        "partprobe": "parted",
        "mkfs.ext4": "e2fsprogs",
    },
    "pacman": {
        "python": "python",
        "curses": "python",
        "lsblk": "util-linux",
        "blkid": "util-linux",
        "findmnt": "util-linux",
        "mount": "util-linux",
        "umount": "util-linux",
        "wipefs": "util-linux",
        "parted": "parted",
        "partprobe": "parted",
        "mkfs.ext4": "e2fsprogs",
    },
    "zypper": {
        "python": "python3",
        "curses": "python3-curses",
        "lsblk": "util-linux",
        "blkid": "util-linux",
        "findmnt": "util-linux",
        "mount": "util-linux",
        "umount": "util-linux",
        "wipefs": "util-linux",
        "parted": "parted",
        "partprobe": "parted",
        "mkfs.ext4": "e2fsprogs",
    },
    "apk": {
        "python": "python3",
        "curses": "py3-curses",
        "lsblk": "util-linux",
        "blkid": "util-linux",
        "findmnt": "util-linux",
        "mount": "util-linux",
        "umount": "util-linux",
        "wipefs": "util-linux",
        "parted": "parted",
        "partprobe": "parted",
        "mkfs.ext4": "e2fsprogs",
    },
}


@dataclass
class DependencyReport:
    missing_commands: list[str] = field(default_factory=list)
    curses_available: bool = True
    package_manager: str = "unknown"
    install_command: str = ""

    @property
    def ok(self) -> bool:
        return not self.missing_commands and self.curses_available


@dataclass
class BlockNode:
    path: str
    name: str
    node_type: str
    size: int = 0
    fstype: str = ""
    label: str = ""
    uuid: str = ""
    mountpoint: str = ""
    model: str = ""
    serial: str = ""
    transport: str = ""
    removable: bool = False
    parent_name: str = ""


@dataclass
class Disk:
    node: BlockNode
    partitions: list[BlockNode] = field(default_factory=list)
    external_reason: str = ""


@dataclass
class Button:
    label: str
    hotkey: str
    action: str
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0

    def contains(self, x: int, y: int) -> bool:
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2


class StorageError(RuntimeError):
    pass


def detect_package_manager() -> str:
    for manager in ("apt", "dnf", "yum", "pacman", "zypper", "apk"):
        if shutil.which(manager):
            return manager
    return "unknown"


def dependency_report() -> DependencyReport:
    missing = sorted(command for command in REQUIRED_COMMANDS if not shutil.which(command))
    curses_available = True
    try:
        import curses  # noqa: F401
    except ImportError:
        curses_available = False

    manager = detect_package_manager()
    needed_keys = set(missing)
    if not curses_available:
        needed_keys.add("curses")
    if not shutil.which("python3") and not shutil.which("python"):
        needed_keys.add("python")

    install_command = build_install_command(manager, needed_keys)
    return DependencyReport(missing, curses_available, manager, install_command)


def build_install_command(manager: str, missing_keys: Iterable[str]) -> str:
    mapping = PACKAGE_MAP.get(manager, PACKAGE_MAP["apt"])
    packages = sorted({mapping[key] for key in missing_keys if key in mapping})
    if not packages:
        packages = sorted(set(mapping.values()))

    package_text = " ".join(packages)
    if manager == "apt":
        return f"sudo apt update && sudo apt install -y {package_text}"
    if manager == "dnf":
        return f"sudo dnf install -y {package_text}"
    if manager == "yum":
        return f"sudo yum install -y {package_text}"
    if manager == "pacman":
        return f"sudo pacman -Syu --needed {package_text}"
    if manager == "zypper":
        return f"sudo zypper install -y {package_text}"
    if manager == "apk":
        return f"sudo apk add {package_text}"
    return "Install Python 3, util-linux, parted, and e2fsprogs with your distribution's package manager."


def print_dependency_failure(report: DependencyReport) -> None:
    print(f"\n{APP_NAME} cannot start because required dependencies are missing.\n", file=sys.stderr)
    if report.missing_commands:
        print("Missing commands:", file=sys.stderr)
        for command in report.missing_commands:
            print(f"  - {command}", file=sys.stderr)
    if not report.curses_available:
        print("  - Python curses module", file=sys.stderr)
    print(f"\nDetected package manager: {report.package_manager}", file=sys.stderr)
    print("\nExecute this command, then run the application again:\n", file=sys.stderr)
    print(f"  {report.install_command}\n", file=sys.stderr)


def invoking_user() -> str:
    sudo_user = os.environ.get("SUDO_USER", "")
    if sudo_user and sudo_user != "root":
        return sudo_user
    return os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name


def invoking_ids() -> tuple[int, int]:
    user = invoking_user()
    record = pwd.getpwnam(user)
    return record.pw_uid, record.pw_gid


def elevate_if_needed(argv: Sequence[str]) -> None:
    if os.geteuid() == 0:
        return
    sudo = shutil.which("sudo")
    if not sudo:
        raise SystemExit("This program must modify block devices and /etc/fstab. Re-run it as root.")
    print("Administrative privileges are required. Requesting sudo access...")
    os.execv(sudo, [sudo, "--preserve-env=TERM,COLORTERM", sys.executable, str(Path(__file__).resolve()), *argv])


def human_size(value: int) -> str:
    number = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f} {unit}" if unit != "B" else f"{int(number)} B"
        number /= 1024
    return f"{value} B"


def command_output(args: Sequence[str], *, check: bool = True) -> str:
    completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or f"Command failed: {shlex.join(args)}"
        raise StorageError(message)
    return completed.stdout


def findmnt_fstab_syntax_valid(output: str, returncode: int) -> bool:
    """Return True when findmnt accepted an fstab file syntactically.

    util-linux has emitted multiple success summaries across releases,
    including both "0 parse errors" and "Success, no errors or warnings
    detected". A disconnected optional device can also produce reachability
    errors while the fstab grammar itself remains valid.
    """
    parse_match = re.search(r"(?im)^\s*(\d+)\s+parse errors?\b", output)
    if parse_match:
        return int(parse_match.group(1)) == 0

    if re.search(r"(?im)^\s*success,\s*no errors or warnings detected\s*$", output):
        return True

    return returncode == 0


def parse_lsblk_pairs(line: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in shlex.split(line, posix=True):
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        values[key] = value
    return values


def scan_block_nodes() -> list[BlockNode]:
    columns = "NAME,KNAME,PATH,TYPE,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINT,MODEL,SERIAL,TRAN,RM,PKNAME"
    output = command_output(["lsblk", "-P", "-b", "-p", "-o", columns])
    nodes: list[BlockNode] = []
    for line in output.splitlines():
        record = parse_lsblk_pairs(line)
        if not record:
            continue
        nodes.append(
            BlockNode(
                path=record.get("PATH") or record.get("NAME", ""),
                name=record.get("KNAME") or Path(record.get("NAME", "")).name,
                node_type=record.get("TYPE", ""),
                size=int(record.get("SIZE") or 0),
                fstype=record.get("FSTYPE", ""),
                label=record.get("LABEL", ""),
                uuid=record.get("UUID", ""),
                mountpoint=record.get("MOUNTPOINT", ""),
                model=record.get("MODEL", "").strip(),
                serial=record.get("SERIAL", "").strip(),
                transport=record.get("TRAN", "").strip(),
                removable=record.get("RM", "0") == "1",
                parent_name=Path(record.get("PKNAME", "")).name,
            )
        )
    return nodes


def critical_system_disks(nodes: Sequence[BlockNode]) -> set[str]:
    """Return every physical disk backing a critical live filesystem.

    Protection is deliberately redundant. We use both the parsed lsblk parent
    graph and lsblk's reverse-dependency view so plain partitions, LVM,
    dm-crypt, mdraid, and device-mapper stacks all resolve to their physical
    disks. This prevents the active SD card/root disk from ever appearing as a
    format candidate.
    """

    protected: set[str] = set()
    by_path = {os.path.realpath(node.path): node for node in nodes if node.path}
    by_name = {node.name: node for node in nodes if node.name}

    def protect_ancestor_chain(node: Optional[BlockNode]) -> None:
        seen: set[str] = set()
        current = node
        while current is not None and current.name not in seen:
            seen.add(current.name)
            if current.node_type == "disk":
                protected.add(os.path.realpath(current.path))
                return
            current = by_name.get(current.parent_name)

    sources: set[str] = set()
    for mountpoint in CRITICAL_MOUNTS:
        result = subprocess.run(
            ["findmnt", "-rn", "-o", "SOURCE", "--target", mountpoint],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        source = result.stdout.strip().splitlines()
        if source:
            # Btrfs may report /dev/xxx[/subvolume]. Strip the subvolume suffix.
            normalized = source[0].split("[", 1)[0]
            if normalized.startswith("/dev/"):
                sources.add(os.path.realpath(normalized))

    for node in nodes:
        if node.mountpoint in CRITICAL_MOUNTS:
            protect_ancestor_chain(node)
            if node.path.startswith("/dev/"):
                sources.add(os.path.realpath(node.path))

    for source in sources:
        protect_ancestor_chain(by_path.get(source))
        result = subprocess.run(
            ["lsblk", "-s", "-n", "-r", "-p", "-o", "NAME,TYPE", source],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[-1] == "disk":
                protected.add(os.path.realpath(fields[0]))

    # Last-resort root-source protection. This is intentionally conservative:
    # if / is a normal block source, its parent disk must never be selectable.
    root_source = command_output(["findmnt", "-rn", "-o", "SOURCE", "/"], check=False).strip()
    root_source = root_source.split("[", 1)[0]
    if root_source.startswith("/dev/"):
        result = subprocess.run(
            ["lsblk", "-s", "-n", "-r", "-p", "-o", "NAME,TYPE", root_source],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[-1] == "disk":
                protected.add(os.path.realpath(fields[0]))

    return protected

def discover_disks() -> list[Disk]:
    nodes = scan_block_nodes()
    protected = critical_system_disks(nodes)
    disks: list[Disk] = []
    partitions_by_parent: dict[str, list[BlockNode]] = {}
    for node in nodes:
        if node.node_type == "part" and node.parent_name:
            partitions_by_parent.setdefault(node.parent_name, []).append(node)

    for node in nodes:
        if node.node_type != "disk" or node.path in protected:
            continue

        reason = ""
        if node.transport in {"usb", "firewire", "thunderbolt"}:
            reason = node.transport.upper()
        elif node.removable:
            reason = "removable"
        else:
            # Keep non-system disks visible so SATA docks and unusual bridges
            # are usable, but label them clearly as unverified/non-removable.
            reason = "non-system disk"

        partitions = sorted(partitions_by_parent.get(node.name, []), key=lambda item: item.path)
        disks.append(Disk(node=node, partitions=partitions, external_reason=reason))

    return sorted(disks, key=lambda disk: disk.node.path)


def expected_partition_path(disk_path: str, number: int = 1) -> str:
    """Return the conventional Linux device path for a partition number.

    Devices whose base name ends in a digit use a ``p`` separator
    (for example ``/dev/nvme0n1p1`` and ``/dev/mmcblk0p1``). Traditional
    SCSI/SATA/USB names use the number directly (for example ``/dev/sda1``).
    """

    canonical = os.path.realpath(disk_path)
    return f"{canonical}p{number}" if re.search(r"\d$", canonical) else f"{canonical}{number}"


def is_block_device(path: str) -> bool:
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def sysfs_partition_paths(disk_path: str) -> list[str]:
    """Return partition device paths already registered by the kernel."""

    disk_name = Path(os.path.realpath(disk_path)).name
    root = Path("/sys/class/block") / disk_name
    found: list[str] = []
    try:
        for child in root.iterdir():
            if (child / "partition").exists():
                found.append(f"/dev/{child.name}")
    except OSError:
        pass
    return sorted(found)


def locate_first_partition(disk_path: str) -> Optional[str]:
    """Locate partition 1 without trusting only one userspace data source."""

    canonical = os.path.realpath(disk_path)
    disk_name = Path(canonical).name

    # First use lsblk, which understands device-mapper and unusual naming.
    try:
        for node in scan_block_nodes():
            if node.node_type == "part" and node.parent_name == disk_name and is_block_device(node.path):
                return node.path
    except StorageError:
        pass

    # Then inspect the kernel's sysfs block hierarchy directly.
    for candidate in sysfs_partition_paths(canonical):
        if is_block_device(candidate):
            return candidate

    # Finally test the conventional partition-1 path.
    expected = expected_partition_path(canonical, 1)
    if is_block_device(expected):
        return expected
    return None


def partition_table_has_partition(disk_path: str, number: int = 1) -> bool:
    """Check the on-disk table directly, independently of kernel rediscovery."""

    output = command_output(
        ["parted", "--machine", "--script", disk_path, "unit", "s", "print"],
        check=False,
    )
    prefix = f"{number}:"
    return any(line.startswith(prefix) for line in output.splitlines())


def filesystem_mount_settings(device: str, uid: int, gid: int, systemd: bool) -> tuple[str, str, str]:
    fstype = command_output(["blkid", "-s", "TYPE", "-o", "value", device], check=False).strip()
    if not fstype:
        raise StorageError(f"No recognizable filesystem was found on {device}.")

    base = ["defaults", "nofail"]
    if systemd:
        base += ["x-systemd.automount", "x-systemd.device-timeout=10s"]

    passno = "0"
    mount_type = fstype
    if fstype in {"ext2", "ext3", "ext4"}:
        base.append("noatime")
        passno = "2"
    elif fstype in {"xfs", "btrfs"}:
        base.append("noatime")
    elif fstype in {"exfat", "vfat"}:
        base += [f"uid={uid}", f"gid={gid}", "umask=0022"]
    elif fstype in {"ntfs", "ntfs3"}:
        filesystems = Path("/proc/filesystems").read_text(errors="ignore") if Path("/proc/filesystems").exists() else ""
        mount_type = "ntfs3" if "ntfs3" in filesystems else "ntfs"
        base += [f"uid={uid}", f"gid={gid}", "umask=0022"]
    else:
        raise StorageError(
            f"Unsupported filesystem '{fstype}' on {device}. Supported existing filesystems: "
            "ext2/3/4, XFS, Btrfs, exFAT, VFAT, and NTFS."
        )
    return mount_type, ",".join(base), passno


def valid_mountpoint(value: str) -> bool:
    return bool(re.fullmatch(r"/(mnt|srv|media)/[A-Za-z0-9._-]+", value))


def valid_ext4_label(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]{1,16}", value))


def is_systemd_running() -> bool:
    return Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None


def unit_name_for_path(path: str, suffix: str) -> str:
    systemd_escape = shutil.which("systemd-escape")
    if systemd_escape:
        return command_output([systemd_escape, "--path", f"--suffix={suffix}", path]).strip()
    stripped = path.strip("/").replace("-", "\\x2d").replace("/", "-") or "-"
    return f"{stripped}.{suffix}"


def _decode_mountinfo_field(value: str) -> str:
    """Decode octal escapes used by /proc/self/mountinfo."""

    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def _mountinfo_records_for_target(mountpoint: str) -> list[tuple[str, str, str, str]]:
    """Return (major:minor, fstype, source, target) records for an exact target."""

    records: list[tuple[str, str, str, str]] = []
    wanted = os.path.realpath(mountpoint)
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records

    for line in lines:
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            continue
        separator = fields.index("-")
        if separator + 2 >= len(fields):
            continue
        target = _decode_mountinfo_field(fields[4])
        if os.path.realpath(target) != wanted:
            continue
        major_minor = fields[2]
        fstype = fields[separator + 1]
        source = _decode_mountinfo_field(fields[separator + 2])
        records.append((major_minor, fstype, source, target))
    return records



def block_tree_paths(disk_path: str) -> list[str]:
    """Return the selected disk and every kernel-visible descendant device."""

    canonical = os.path.realpath(disk_path)
    completed = subprocess.run(
        ["lsblk", "-n", "-r", "-p", "-o", "PATH", canonical],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    paths: list[str] = []
    for line in completed.stdout.splitlines():
        value = line.strip()
        if value.startswith("/dev/"):
            paths.append(os.path.realpath(value))
    if canonical not in paths:
        paths.insert(0, canonical)
    return list(dict.fromkeys(paths))


def direct_partition_paths(disk_path: str) -> list[str]:
    """Return direct partition children, including sysfs-only children."""

    canonical = os.path.realpath(disk_path)
    disk_name = Path(canonical).name
    found: list[str] = []
    try:
        completed = subprocess.run(
            ["lsblk", "-n", "-r", "-p", "-o", "PATH,TYPE,PKNAME", canonical],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] == "part" and Path(fields[2]).name == disk_name:
                found.append(os.path.realpath(fields[0]))
    except OSError:
        pass
    found.extend(os.path.realpath(path) for path in sysfs_partition_paths(canonical))
    expected = expected_partition_path(canonical, 1)
    if is_block_device(expected):
        found.append(os.path.realpath(expected))
    return list(dict.fromkeys(found))


def resolve_fstab_source(source: str) -> str:
    """Resolve a common fstab source specification to a canonical device path."""

    source = source.strip()
    if source.startswith("/dev/"):
        return os.path.realpath(source)
    commands: list[list[str]] = []
    if source.startswith("UUID="):
        commands.append(["blkid", "-U", source.split("=", 1)[1]])
    elif source.startswith("LABEL="):
        commands.append(["blkid", "-L", source.split("=", 1)[1]])
    elif source.startswith("PARTUUID="):
        commands.append(["blkid", "-t", source, "-o", "device"])
    elif source.startswith("PARTLABEL="):
        commands.append(["blkid", "-t", source, "-o", "device"])
    for command in commands:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        candidate = completed.stdout.strip().splitlines()
        if candidate and candidate[0].startswith("/dev/"):
            return os.path.realpath(candidate[0])
    return ""


def fstab_records_for_devices(device_paths: Sequence[str]) -> list[tuple[str, str]]:
    """Return fstab (source, target) records backed by any selected device."""

    wanted = {os.path.realpath(path) for path in device_paths if path.startswith("/dev/")}
    records: list[tuple[str, str]] = []
    try:
        lines = FSTAB.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) < 2:
            continue
        source, target = fields[0], fields[1]
        resolved = resolve_fstab_source(source)
        if resolved and resolved in wanted:
            records.append((source, target))
    return records


def live_mount_targets_for_devices(device_paths: Sequence[str]) -> list[str]:
    """Return all active mount targets whose source is one of the devices."""

    targets: list[str] = []
    for path in device_paths:
        if not path.startswith("/dev/"):
            continue
        completed = subprocess.run(
            ["findmnt", "-r", "-n", "--source", path, "-o", "TARGET"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        for line in completed.stdout.splitlines():
            target = line.strip()
            if target.startswith("/"):
                targets.append(target)
    return list(dict.fromkeys(targets))


def fstab_records_for_targets(targets: Sequence[str]) -> list[tuple[str, str]]:
    """Return fstab records whose target matches an active selected-disk mount."""

    wanted = {os.path.realpath(target) for target in targets if target.startswith("/")}
    records: list[tuple[str, str]] = []
    try:
        lines = FSTAB.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return records
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        fields = stripped.split()
        if len(fields) >= 2 and fields[1].startswith("/") and os.path.realpath(fields[1]) in wanted:
            records.append((fields[0], fields[1]))
    return records


def verify_device_mounted_at(device: str, mountpoint: str) -> tuple[bool, str]:
    """Verify the real block filesystem, not merely a systemd autofs trigger.

    A systemd automount creates an autofs layer whose source is commonly
    reported as ``systemd-1``.  It may coexist at the same target with the
    actual block-device mount.  Therefore a target-only ``findmnt`` query is
    ambiguous.  Match the expected source and exact mount point first, then
    fall back to the kernel mount table's major:minor identity.
    """

    expected = os.path.realpath(device)
    uuid = command_output(["blkid", "-s", "UUID", "-o", "value", device], check=False).strip()
    source_specs = [device]
    if uuid:
        source_specs.append(f"UUID={uuid}")

    observed: list[str] = []
    for source_spec in source_specs:
        completed = subprocess.run(
            [
                "findmnt",
                "-r",
                "-n",
                "--source",
                source_spec,
                "--mountpoint",
                mountpoint,
                "-o",
                "SOURCE,FSTYPE,TARGET",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for line in completed.stdout.splitlines():
            fields = line.split(None, 2)
            if len(fields) != 3:
                continue
            source, fstype, target = fields
            observed.append(f"{source} {fstype} {target}")
            if fstype == "autofs" or source == "systemd-1":
                continue
            if os.path.realpath(target) != os.path.realpath(mountpoint):
                continue
            if source.startswith("/dev/") and os.path.realpath(source) == expected:
                return True, f"{source} ({fstype})"
            if uuid and source == f"UUID={uuid}":
                return True, f"{source} ({fstype})"

    # The kernel's mountinfo major:minor value is the most authoritative way to
    # identify a block-backed mount even when SOURCE is a symlink or a tag.
    try:
        device_stat = os.stat(expected)
        expected_major_minor = f"{os.major(device_stat.st_rdev)}:{os.minor(device_stat.st_rdev)}"
    except OSError:
        expected_major_minor = ""

    for major_minor, fstype, source, target in _mountinfo_records_for_target(mountpoint):
        observed.append(f"{source} {fstype} {target} [{major_minor}]")
        if fstype != "autofs" and expected_major_minor and major_minor == expected_major_minor:
            return True, f"{source} ({fstype}, {major_minor})"

    if not observed:
        return False, "no exact mount record found"
    # Preserve order while removing duplicate diagnostics.
    unique = list(dict.fromkeys(observed))
    return False, "; ".join(unique)


class TuiApp:
    def __init__(self, stdscr) -> None:
        import curses

        self.curses = curses
        self.stdscr = stdscr
        self.disks: list[Disk] = []
        self.selected_index = 0
        self.focus = "devices"
        self.selected_button = 0
        self.buttons = [
            Button("Refresh", "R", "refresh"),
            Button("Mount existing", "M", "mount"),
            Button("Format ext4", "F", "format"),
            Button("Quit", "Q", "quit"),
        ]
        self.logs: list[str] = []
        self.status = "Starting environment scan..."
        self.mouse_available = False
        self.running = True
        self.systemd = is_systemd_running()
        self.user = invoking_user()
        self.uid, self.gid = invoking_ids()
        self._configure_terminal()

    def _configure_terminal(self) -> None:
        c = self.curses
        self.stdscr.keypad(True)
        c.curs_set(0)
        try:
            c.start_color()
            c.use_default_colors()
            c.init_pair(1, c.COLOR_GREEN, -1)
            c.init_pair(2, c.COLOR_YELLOW, -1)
            c.init_pair(3, c.COLOR_RED, -1)
            c.init_pair(4, c.COLOR_CYAN, -1)
        except c.error:
            pass
        try:
            available, _ = c.mousemask(c.ALL_MOUSE_EVENTS | getattr(c, "REPORT_MOUSE_POSITION", 0))
            self.mouse_available = bool(available)
            c.mouseinterval(120)
        except c.error:
            self.mouse_available = False

    def log(self, message: str, level: str = "INFO") -> None:
        stamp = dt.datetime.now().strftime("%H:%M:%S")
        line = f"{stamp} [{level}] {message}"
        self.logs.append(line)
        self.logs = self.logs[-500:]
        try:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as stream:
                stream.write(f"{dt.datetime.now().isoformat(timespec='seconds')} {line}\n")
            os.chmod(LOG_FILE, 0o600)
        except OSError:
            pass

    def set_status(self, message: str) -> None:
        self.status = message
        self.log(message)
        self.draw()

    def refresh_devices(self) -> None:
        self.set_status("Scanning Linux block devices with lsblk...")
        self.disks = discover_disks()
        self.selected_index = min(self.selected_index, max(0, len(self.disks) - 1))
        if self.disks:
            self.status = f"Detected {len(self.disks)} eligible non-system disk(s)."
            self.log(self.status, "OK")
        else:
            self.status = "No eligible non-system storage devices were detected."
            self.log(self.status, "WARN")

    def run(self) -> None:
        self.log(f"Starting {APP_NAME} v{VERSION} build {BUILD_ID} as root for user {self.user}.")
        self.log(f"Init system: {'systemd' if self.systemd else 'non-systemd/generic fstab mode'}.")
        self.refresh_devices()
        while self.running:
            self.draw()
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                break
            self.handle_key(key)

    def safe_addstr(self, y: int, x: int, text: str, attr: int = 0, max_width: Optional[int] = None) -> None:
        height, width = self.stdscr.getmaxyx()
        if y < 0 or y >= height or x < 0 or x >= width:
            return
        allowed = width - x - 1
        if max_width is not None:
            allowed = min(allowed, max_width)
        if allowed <= 0:
            return
        try:
            self.stdscr.addnstr(y, x, text, allowed, attr)
        except self.curses.error:
            pass

    def draw_border(self, y: int, x: int, h: int, w: int, title: str = "") -> None:
        c = self.curses
        if h < 2 or w < 2:
            return
        try:
            self.stdscr.addch(y, x, c.ACS_ULCORNER)
            self.stdscr.hline(y, x + 1, c.ACS_HLINE, w - 2)
            self.stdscr.addch(y, x + w - 1, c.ACS_URCORNER)
            self.stdscr.vline(y + 1, x, c.ACS_VLINE, h - 2)
            self.stdscr.vline(y + 1, x + w - 1, c.ACS_VLINE, h - 2)
            self.stdscr.addch(y + h - 1, x, c.ACS_LLCORNER)
            self.stdscr.hline(y + h - 1, x + 1, c.ACS_HLINE, w - 2)
            self.stdscr.addch(y + h - 1, x + w - 1, c.ACS_LRCORNER)
        except c.error:
            return
        if title:
            self.safe_addstr(y, x + 2, f" {title} ", c.A_BOLD, w - 4)

    def draw(self) -> None:
        c = self.curses
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        if height < 22 or width < 88:
            self.safe_addstr(0, 0, f"Terminal too small ({width}x{height}). Resize to at least 88x22.", c.A_BOLD)
            self.stdscr.refresh()
            return

        title = f"{APP_NAME}  v{VERSION}  build={BUILD_ID}"
        right_title = f"user={self.user}  init={'systemd' if self.systemd else 'generic'}"
        self.safe_addstr(0, 1, title, c.A_BOLD)
        self.safe_addstr(0, max(1, width - len(right_title) - 2), right_title, c.A_DIM)
        self.safe_addstr(1, 1, "Persistent UUID mounts for external and secondary Linux storage", c.color_pair(4))

        top = 3
        log_height = max(6, min(10, height // 3))
        button_height = 3
        main_height = height - top - log_height - button_height - 2
        left_width = max(38, width // 2)
        right_width = width - left_width - 1

        self.draw_border(top, 0, main_height, left_width, "Storage devices")
        self.draw_border(top, left_width, main_height, right_width, "Selected device")
        self.draw_device_list(top + 1, 1, main_height - 2, left_width - 2)
        self.draw_details(top + 1, left_width + 1, main_height - 2, right_width - 2)

        log_y = top + main_height
        self.draw_border(log_y, 0, log_height, width, "Live operation log")
        visible_logs = self.logs[-(log_height - 2):]
        for index, line in enumerate(visible_logs):
            attr = 0
            if "[OK]" in line:
                attr = c.color_pair(1)
            elif "[WARN]" in line:
                attr = c.color_pair(2)
            elif "[ERROR]" in line or "[FATAL]" in line:
                attr = c.color_pair(3)
            self.safe_addstr(log_y + 1 + index, 1, line, attr, width - 2)

        button_y = log_y + log_height
        self.draw_buttons(button_y, width)
        footer = "↑/↓ select  Tab switch focus  Enter activate  R refresh  M mount  F format  Q quit"
        mouse_text = "mouse enabled" if self.mouse_available else "mouse unavailable in this terminal"
        self.safe_addstr(height - 1, 1, footer, c.A_DIM, width - len(mouse_text) - 4)
        self.safe_addstr(height - 1, max(1, width - len(mouse_text) - 2), mouse_text, c.A_DIM)
        self.stdscr.refresh()

    def draw_device_list(self, y: int, x: int, h: int, w: int) -> None:
        c = self.curses
        if not self.disks:
            self.safe_addstr(y + 1, x + 1, "No non-system disks found.", c.color_pair(2), w - 2)
            self.safe_addstr(y + 3, x + 1, "Connect a USB/removable disk and press R.", c.A_DIM, w - 2)
            return
        for row, disk in enumerate(self.disks[:h]):
            node = disk.node
            marker = "▶" if row == self.selected_index else " "
            transport = disk.external_reason
            text = f"{marker} {node.path:<10} {human_size(node.size):>10}  {transport:<15}"
            attr = c.A_REVERSE | c.A_BOLD if row == self.selected_index and self.focus == "devices" else 0
            if row == self.selected_index and self.focus != "devices":
                attr = c.A_BOLD
            self.safe_addstr(y + row, x, text, attr, w)

    def draw_details(self, y: int, x: int, h: int, w: int) -> None:
        c = self.curses
        if not self.disks:
            return
        disk = self.disks[self.selected_index]
        node = disk.node
        details = [
            ("Device", node.path),
            ("Model", node.model or "unknown"),
            ("Serial", node.serial or "not reported"),
            ("Capacity", human_size(node.size)),
            ("Transport", node.transport or "not reported"),
            ("Classification", disk.external_reason),
            ("Partitions", str(len(disk.partitions))),
        ]
        row = y
        for label, value in details:
            self.safe_addstr(row, x + 1, f"{label}:", c.A_BOLD, 16)
            self.safe_addstr(row, x + 18, value, 0, max(1, w - 19))
            row += 1
        row += 1
        self.safe_addstr(row, x + 1, "Filesystems", c.A_BOLD | c.A_UNDERLINE, w - 2)
        row += 1
        if not disk.partitions:
            self.safe_addstr(row, x + 1, "No partitions detected.", c.A_DIM, w - 2)
        else:
            for part in disk.partitions:
                if row >= y + h:
                    break
                first = f"{part.path}  {human_size(part.size)}  {part.fstype or 'unformatted'}"
                self.safe_addstr(row, x + 1, first, 0, w - 2)
                row += 1
                if row >= y + h:
                    break
                second = f"label={part.label or '-'}  uuid={part.uuid or '-'}"
                self.safe_addstr(row, x + 3, second, c.A_DIM, w - 4)
                row += 1
                if part.mountpoint and row < y + h:
                    self.safe_addstr(row, x + 3, f"mounted at {part.mountpoint}", c.color_pair(1), w - 4)
                    row += 1
        if row < y + h:
            row = y + h - 2
            self.safe_addstr(row, x + 1, f"Status: {self.status}", c.A_DIM, w - 2)

    def draw_buttons(self, y: int, width: int) -> None:
        c = self.curses
        x = 1
        for index, button in enumerate(self.buttons):
            label = f"[ {button.label} ]"
            button.x1, button.y1 = x, y
            button.x2, button.y2 = x + len(label) - 1, y
            attr = c.A_REVERSE | c.A_BOLD if self.focus == "buttons" and index == self.selected_button else c.A_BOLD
            if button.action == "format":
                attr |= c.color_pair(3)
            self.safe_addstr(y, x, label, attr)
            x += len(label) + 2

    def handle_key(self, key: int) -> None:
        c = self.curses
        if key == c.KEY_RESIZE:
            return
        if key == c.KEY_MOUSE:
            self.handle_mouse()
            return
        if key in (ord("q"), ord("Q"), 27):
            self.running = False
            return
        if key in (ord("r"), ord("R")):
            self.refresh_devices()
            return
        if key in (ord("m"), ord("M")):
            self.mount_existing()
            return
        if key in (ord("f"), ord("F")):
            self.format_ext4()
            return
        if key == 9:  # Tab
            self.focus = "buttons" if self.focus == "devices" else "devices"
            return
        if self.focus == "devices":
            if key in (c.KEY_UP, ord("k")) and self.disks:
                self.selected_index = (self.selected_index - 1) % len(self.disks)
            elif key in (c.KEY_DOWN, ord("j")) and self.disks:
                self.selected_index = (self.selected_index + 1) % len(self.disks)
            elif key in (10, 13, c.KEY_ENTER):
                self.mount_existing()
        else:
            if key in (c.KEY_LEFT, ord("h")):
                self.selected_button = (self.selected_button - 1) % len(self.buttons)
            elif key in (c.KEY_RIGHT, ord("l")):
                self.selected_button = (self.selected_button + 1) % len(self.buttons)
            elif key in (10, 13, c.KEY_ENTER):
                self.activate_button(self.buttons[self.selected_button].action)

    def handle_mouse(self) -> None:
        c = self.curses
        try:
            _, x, y, _, state = c.getmouse()
        except c.error:
            return
        if state & getattr(c, "BUTTON4_PRESSED", 0):
            if self.disks:
                self.selected_index = max(0, self.selected_index - 1)
            return
        if state & getattr(c, "BUTTON5_PRESSED", 0):
            if self.disks:
                self.selected_index = min(len(self.disks) - 1, self.selected_index + 1)
            return
        click_mask = (
            getattr(c, "BUTTON1_CLICKED", 0)
            | getattr(c, "BUTTON1_RELEASED", 0)
            | getattr(c, "BUTTON1_PRESSED", 0)
        )
        if not state & click_mask:
            return
        height, width = self.stdscr.getmaxyx()
        top = 3
        log_height = max(6, min(10, height // 3))
        main_height = height - top - log_height - 3 - 2
        left_width = max(38, width // 2)
        if top + 1 <= y < top + main_height - 1 and 1 <= x < left_width - 1:
            index = y - (top + 1)
            if 0 <= index < len(self.disks):
                self.selected_index = index
                self.focus = "devices"
                return
        for index, button in enumerate(self.buttons):
            if button.contains(x, y):
                self.focus = "buttons"
                self.selected_button = index
                self.activate_button(button.action)
                return

    def activate_button(self, action: str) -> None:
        if action == "refresh":
            self.refresh_devices()
        elif action == "mount":
            self.mount_existing()
        elif action == "format":
            self.format_ext4()
        elif action == "quit":
            self.running = False

    def modal_box(self, title: str, lines: Sequence[str], *, kind: str = "normal", footer: str = "Press Enter") -> None:
        c = self.curses
        wrapped: list[str] = []
        _, screen_width = self.stdscr.getmaxyx()
        wrap_width = max(40, min(92, screen_width - 10))
        for line in lines:
            wrapped.extend(textwrap.wrap(line, wrap_width) or [""])
        box_width = min(screen_width - 4, max(56, max((len(line) for line in wrapped), default=20) + 4))
        screen_height, _ = self.stdscr.getmaxyx()
        box_height = min(screen_height - 4, max(8, len(wrapped) + 5))
        y = max(1, (screen_height - box_height) // 2)
        x = max(1, (screen_width - box_width) // 2)
        attr = c.A_BOLD
        if kind == "error":
            attr |= c.color_pair(3)
        elif kind == "warning":
            attr |= c.color_pair(2)
        elif kind == "success":
            attr |= c.color_pair(1)
        while True:
            self.draw()
            self.draw_border(y, x, box_height, box_width, title)
            for index, line in enumerate(wrapped[: box_height - 4]):
                self.safe_addstr(y + 2 + index, x + 2, line, attr if index == 0 else 0, box_width - 4)
            self.safe_addstr(y + box_height - 2, x + 2, footer, c.A_REVERSE, box_width - 4)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (10, 13, c.KEY_ENTER, 27, ord("q"), ord("Q")):
                return

    def confirm(self, title: str, lines: Sequence[str], *, destructive: bool = False) -> bool:
        c = self.curses
        choice = 1  # Default No.
        while True:
            self.draw()
            height, width = self.stdscr.getmaxyx()
            wrapped: list[str] = []
            for line in lines:
                wrapped.extend(textwrap.wrap(line, min(84, width - 12)) or [""])
            box_width = min(width - 4, max(58, max((len(line) for line in wrapped), default=20) + 4))
            box_height = min(height - 4, len(wrapped) + 7)
            y = (height - box_height) // 2
            x = (width - box_width) // 2
            self.draw_border(y, x, box_height, box_width, title)
            for index, line in enumerate(wrapped[: box_height - 5]):
                attr = c.color_pair(3) | c.A_BOLD if destructive and index == 0 else 0
                self.safe_addstr(y + 2 + index, x + 2, line, attr, box_width - 4)
            yes = "[ Yes ]"
            no = "[ No ]"
            button_y = y + box_height - 2
            yes_x = x + box_width // 2 - len(yes) - 2
            no_x = x + box_width // 2 + 2
            self.safe_addstr(button_y, yes_x, yes, c.A_REVERSE if choice == 0 else c.A_BOLD)
            self.safe_addstr(button_y, no_x, no, c.A_REVERSE if choice == 1 else c.A_BOLD)
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (c.KEY_LEFT, c.KEY_RIGHT, 9):
                choice = 1 - choice
            elif key in (ord("y"), ord("Y")):
                return True
            elif key in (ord("n"), ord("N"), 27):
                return False
            elif key in (10, 13, c.KEY_ENTER):
                return choice == 0
            elif key == c.KEY_MOUSE:
                try:
                    _, mx, my, _, state = c.getmouse()
                except c.error:
                    continue
                if state & (getattr(c, "BUTTON1_CLICKED", 0) | getattr(c, "BUTTON1_RELEASED", 0)):
                    if button_y == my and yes_x <= mx < yes_x + len(yes):
                        return True
                    if button_y == my and no_x <= mx < no_x + len(no):
                        return False

    def input_dialog(
        self,
        title: str,
        prompt: Sequence[str],
        default: str = "",
        validator: Optional[Callable[[str], bool]] = None,
        validation_message: str = "Invalid input.",
    ) -> Optional[str]:
        c = self.curses
        value = list(default)
        cursor = len(value)
        while True:
            self.draw()
            height, width = self.stdscr.getmaxyx()
            wrapped: list[str] = []
            for line in prompt:
                wrapped.extend(textwrap.wrap(line, min(82, width - 12)) or [""])
            box_width = min(width - 4, max(62, max((len(line) for line in wrapped), default=30) + 4))
            box_height = min(height - 4, len(wrapped) + 8)
            y = (height - box_height) // 2
            x = (width - box_width) // 2
            self.draw_border(y, x, box_height, box_width, title)
            for index, line in enumerate(wrapped):
                self.safe_addstr(y + 2 + index, x + 2, line, 0, box_width - 4)
            input_y = y + 3 + len(wrapped)
            input_x = x + 2
            input_width = box_width - 4
            try:
                self.stdscr.move(input_y, input_x)
                self.stdscr.clrtoeol()
            except c.error:
                pass
            rendered = "".join(value)
            self.safe_addstr(input_y, input_x, rendered, c.A_REVERSE, input_width)
            self.safe_addstr(y + box_height - 2, x + 2, "Enter accept   Esc cancel", c.A_DIM, box_width - 4)
            try:
                c.curs_set(1)
                self.stdscr.move(input_y, min(input_x + cursor, input_x + input_width - 1))
            except c.error:
                pass
            self.stdscr.refresh()
            key = self.stdscr.getch()
            if key in (10, 13, c.KEY_ENTER):
                result = "".join(value).strip()
                if validator is None or validator(result):
                    try:
                        c.curs_set(0)
                    except c.error:
                        pass
                    return result
                self.modal_box("Input rejected", [validation_message], kind="error")
            elif key == 27:
                try:
                    c.curs_set(0)
                except c.error:
                    pass
                return None
            elif key in (c.KEY_BACKSPACE, 127, 8):
                if cursor > 0:
                    value.pop(cursor - 1)
                    cursor -= 1
            elif key == c.KEY_DC:
                if cursor < len(value):
                    value.pop(cursor)
            elif key == c.KEY_LEFT:
                cursor = max(0, cursor - 1)
            elif key == c.KEY_RIGHT:
                cursor = min(len(value), cursor + 1)
            elif key == c.KEY_HOME:
                cursor = 0
            elif key == c.KEY_END:
                cursor = len(value)
            elif 32 <= key <= 126 and len(value) < input_width - 1:
                value.insert(cursor, chr(key))
                cursor += 1

    def select_dialog(self, title: str, items: Sequence[tuple[str, str]]) -> Optional[str]:
        c = self.curses
        if not items:
            return None
        selected = 0
        while True:
            self.draw()
            height, width = self.stdscr.getmaxyx()
            box_width = min(width - 4, max(70, max(len(a) + len(b) + 5 for a, b in items) + 4))
            box_height = min(height - 4, max(10, min(len(items), height - 10) + 5))
            y = (height - box_height) // 2
            x = (width - box_width) // 2
            self.draw_border(y, x, box_height, box_width, title)
            visible = box_height - 4
            start = max(0, selected - visible + 1)
            for row, (key_value, description) in enumerate(items[start : start + visible]):
                absolute = start + row
                text = f"{key_value}  {description}"
                attr = c.A_REVERSE | c.A_BOLD if absolute == selected else 0
                self.safe_addstr(y + 2 + row, x + 2, text, attr, box_width - 4)
            self.safe_addstr(y + box_height - 2, x + 2, "↑/↓ select   Enter accept   Esc cancel", c.A_DIM, box_width - 4)
            self.stdscr.refresh()
            event = self.stdscr.getch()
            if event in (c.KEY_UP, ord("k")):
                selected = (selected - 1) % len(items)
            elif event in (c.KEY_DOWN, ord("j")):
                selected = (selected + 1) % len(items)
            elif event in (10, 13, c.KEY_ENTER):
                return items[selected][0]
            elif event == 27:
                return None
            elif event == c.KEY_MOUSE:
                try:
                    _, mx, my, _, state = c.getmouse()
                except c.error:
                    continue
                if state & (getattr(c, "BUTTON1_CLICKED", 0) | getattr(c, "BUTTON1_RELEASED", 0)):
                    clicked = start + (my - (y + 2))
                    if x + 1 <= mx < x + box_width - 1 and 0 <= clicked < len(items):
                        selected = clicked
                        return items[selected][0]

    def operation_screen(self, title: str, steps: Sequence[str], active: int, output: Sequence[str]) -> None:
        c = self.curses
        self.stdscr.erase()
        height, width = self.stdscr.getmaxyx()
        self.safe_addstr(0, 1, APP_NAME, c.A_BOLD)
        self.safe_addstr(1, 1, title, c.color_pair(4) | c.A_BOLD, width - 2)
        self.draw_border(3, 0, max(8, len(steps) + 4), width, "Execution plan")
        for index, step in enumerate(steps):
            if index < active:
                marker, attr = "✓", c.color_pair(1)
            elif index == active:
                marker, attr = "▶", c.color_pair(2) | c.A_BOLD
            else:
                marker, attr = "·", c.A_DIM
            self.safe_addstr(5 + index, 2, f"{marker} Step {index + 1}/{len(steps)} — {step}", attr, width - 4)
        output_y = max(8, len(steps) + 7)
        output_h = height - output_y - 2
        if output_h >= 4:
            self.draw_border(output_y, 0, output_h, width, "Command output")
            for row, line in enumerate(output[-(output_h - 2):]):
                self.safe_addstr(output_y + 1 + row, 1, line, 0, width - 2)
        self.safe_addstr(height - 1, 1, "Destructive steps cannot be cancelled once started.", c.A_DIM, width - 2)
        self.stdscr.refresh()

    def run_steps(self, title: str, steps: Sequence[tuple[str, Sequence[str]]]) -> None:
        output: list[str] = []
        names = [name for name, _ in steps]
        for index, (name, command) in enumerate(steps):
            self.status = name
            self.log(f"START: {name}: {shlex.join(command)}")
            self.operation_screen(title, names, index, output)
            process = subprocess.Popen(
                command,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                clean = line.rstrip()
                if clean:
                    output.append(clean)
                    output = output[-250:]
                    self.log(clean, "CMD")
                    self.operation_screen(title, names, index, output)
            returncode = process.wait()
            if returncode != 0:
                self.log(f"FAILED ({returncode}): {name}", "ERROR")
                self.operation_screen(title, names, index, output)
                raise StorageError(f"{name} failed with exit code {returncode}.\n\n" + "\n".join(output[-20:]))
            self.log(f"DONE: {name}", "OK")
            self.operation_screen(title, names, index + 1, output)
        time.sleep(0.4)

    def selected_disk(self) -> Optional[Disk]:
        if not self.disks:
            self.modal_box("No storage device", ["No eligible non-system disk is available. Connect a device and press R."], kind="warning")
            return None
        return self.disks[self.selected_index]

    def run_best_effort(self, description: str, command: Sequence[str]) -> bool:
        """Run a recovery command, log all output, and never mask later fallbacks."""

        if not command or shutil.which(command[0]) is None:
            self.log(f"SKIP: {description}; {command[0] if command else 'command'} is unavailable.", "WARN")
            return False
        self.log(f"RECOVERY: {description}: {shlex.join(command)}", "INFO")
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        output = (completed.stdout or "").strip()
        for line in output.splitlines():
            self.log(line, "CMD")
        level = "OK" if completed.returncode == 0 else "WARN"
        self.log(f"RECOVERY {'DONE' if completed.returncode == 0 else 'EXIT ' + str(completed.returncode)}: {description}", level)
        return completed.returncode == 0

    def wait_for_partition_node(self, disk_path: str, timeout: float = 60.0) -> str:
        """Force and verify kernel discovery of partition 1.

        Some USB-to-SATA bridges commit the GPT immediately but delay, or fail,
        the BLKRRPART notification that creates /dev/sdX1. This routine does not
        trust a single lsblk refresh. It verifies the on-disk GPT, performs
        several independent reread paths, polls sysfs and /proc, and can recreate
        a missing /dev node from the kernel-reported major/minor number.
        """

        canonical = os.path.realpath(disk_path)
        expected = expected_partition_path(canonical, 1)
        disk_name = Path(canonical).name
        self.log(
            f"PARTITION-DISCOVERY: v{VERSION} build {BUILD_ID}; disk={canonical}; expected={expected}",
            "INFO",
        )

        if not partition_table_has_partition(canonical, 1):
            table = command_output(
                ["parted", "--machine", "--script", canonical, "unit", "s", "print"],
                check=False,
            ).strip()
            raise StorageError(
                f"parted returned success, but partition 1 is absent from the on-disk GPT for {canonical}.\n\n"
                f"parted output:\n{table or '(no output)'}"
            )

        self.log(f"Verified partition 1 exists in the GPT on {canonical}.", "OK")

        def recreate_devnode_from_sysfs() -> Optional[str]:
            candidates = [Path(expected).name]
            candidates.extend(Path(path).name for path in sysfs_partition_paths(canonical))
            for name in dict.fromkeys(candidates):
                sys_dev = Path("/sys/class/block") / name / "dev"
                partition_flag = Path("/sys/class/block") / name / "partition"
                if not sys_dev.exists() or not partition_flag.exists():
                    continue
                target = Path("/dev") / name
                if is_block_device(str(target)):
                    return str(target)
                try:
                    major_s, minor_s = sys_dev.read_text(encoding="ascii").strip().split(":", 1)
                    mode = stat.S_IFBLK | 0o660
                    os.mknod(target, mode, os.makedev(int(major_s), int(minor_s)))
                    try:
                        import grp

                        os.chown(target, 0, grp.getgrnam("disk").gr_gid)
                    except (KeyError, OSError):
                        pass
                    self.log(
                        f"Created missing device node {target} from sysfs major:minor {major_s}:{minor_s}.",
                        "OK",
                    )
                    return str(target) if is_block_device(str(target)) else None
                except (OSError, ValueError) as exc:
                    self.log(f"Could not recreate {target} from sysfs: {exc}", "WARN")
            return None

        def refresh_kernel(cycle: int) -> None:
            self.log(f"Partition refresh cycle {cycle} started.", "INFO")
            try:
                os.sync()
            except AttributeError:
                pass

            self.run_best_effort("Reload partition table with partprobe", ["partprobe", canonical])
            self.run_best_effort("Reread partition table with blockdev", ["blockdev", "--rereadpt", canonical])
            updated = self.run_best_effort("Update kernel partition mappings with partx", ["partx", "--update", canonical])
            if not updated:
                self.run_best_effort("Add kernel partition mappings with partx", ["partx", "--add", canonical])

            rescan = Path("/sys/class/block") / disk_name / "device" / "rescan"
            if rescan.exists():
                try:
                    rescan.write_text("1\n", encoding="ascii")
                    self.log(f"Requested selected-device rescan through {rescan}.", "OK")
                except OSError as exc:
                    self.log(f"Selected-device rescan failed through {rescan}: {exc}", "WARN")

            if cycle >= 3:
                for scan in sorted(Path("/sys/class/scsi_host").glob("host*/scan")):
                    try:
                        scan.write_text("- - -\n", encoding="ascii")
                        self.log(f"Requested SCSI host rescan through {scan}.", "OK")
                    except OSError as exc:
                        self.log(f"SCSI host rescan failed through {scan}: {exc}", "WARN")

            if shutil.which("udevadm"):
                self.run_best_effort(
                    "Trigger block-device udev change events",
                    ["udevadm", "trigger", "--subsystem-match=block", "--action=change"],
                )
                self.run_best_effort("Wait for udev to settle", ["udevadm", "settle", "--timeout=10"])

        deadline = time.monotonic() + timeout
        cycle = 0
        next_refresh = 0.0
        while time.monotonic() < deadline:
            candidate = locate_first_partition(canonical)
            if candidate:
                self.log(f"Kernel exposed partition 1 as {candidate}.", "OK")
                return candidate

            recreated = recreate_devnode_from_sysfs()
            if recreated:
                self.log(f"Partition 1 is available as {recreated}.", "OK")
                return recreated

            now = time.monotonic()
            if now >= next_refresh:
                cycle += 1
                remaining = max(0, int(deadline - now))
                self.status = f"Discovering {expected}: cycle {cycle}, {remaining}s remaining"
                self.log(self.status, "INFO")
                self.draw()
                self.stdscr.refresh()
                refresh_kernel(cycle)
                next_refresh = time.monotonic() + 5.0
            time.sleep(0.5)

        parted_view = command_output(
            ["parted", "--script", canonical, "unit", "s", "print"], check=False
        ).strip()
        lsblk_view = command_output(
            ["lsblk", "-o", "NAME,PATH,TYPE,SIZE,FSTYPE,UUID,PARTUUID,MOUNTPOINTS", canonical],
            check=False,
        ).strip()
        partx_view = command_output(["partx", "--show", canonical], check=False).strip()
        proc_lines: list[str] = []
        try:
            for line in Path("/proc/partitions").read_text(errors="replace").splitlines():
                if disk_name in line:
                    proc_lines.append(line.strip())
        except OSError:
            pass
        sysfs_view = "\n".join(sysfs_partition_paths(canonical)) or "(none)"

        raise StorageError(
            f"Partition 1 exists in the GPT, but Linux did not expose a usable block node after {int(timeout)} seconds.\n\n"
            f"Application: v{VERSION} build {BUILD_ID}\n"
            f"Expected node: {expected}\n\n"
            f"parted view:\n{parted_view or '(no output)'}\n\n"
            f"partx view:\n{partx_view or '(no output)'}\n\n"
            f"lsblk view:\n{lsblk_view or '(no output)'}\n\n"
            f"sysfs partition paths:\n{sysfs_view}\n\n"
            f"/proc/partitions:\n{chr(10).join(proc_lines) or '(no matching entries)'}"
        )

    def mounted_descendants(self, disk: Disk) -> list[BlockNode]:
        return [part for part in disk.partitions if part.mountpoint]

    def prepare_disk_for_repartition(self, disk: Disk) -> tuple[list[str], list[str]]:
        """Dismantle every mount, automount, and swap use of a selected disk.

        A generated systemd automount can keep a filesystem active even when
        the device list was refreshed after its partition table changed.  The
        formatter therefore derives users from the live kernel tree and fstab,
        not only from the TUI's cached partition list.

        Returns the old fstab source specifications and direct partitions so
        stale UUID records can be removed when the replacement entry is saved.
        """

        disk_path = os.path.realpath(disk.node.path)
        device_paths = block_tree_paths(disk_path)
        partitions = direct_partition_paths(disk_path)
        device_paths = list(dict.fromkeys([*device_paths, *partitions]))
        live_targets = live_mount_targets_for_devices(device_paths)
        fstab_records = fstab_records_for_devices(device_paths)
        fstab_records.extend(fstab_records_for_targets(live_targets))
        fstab_records = list(dict.fromkeys(fstab_records))
        fstab_sources = [source for source, _ in fstab_records]
        targets = list(live_targets)
        targets.extend(target for _, target in fstab_records)
        targets = sorted(set(targets), key=lambda value: (value.count("/"), len(value)), reverse=True)

        self.log(
            f"BUSY-DEVICE-PREFLIGHT: v{VERSION} build {BUILD_ID}; disk={disk_path}; "
            f"devices={','.join(device_paths) or '(none)'}",
            "INFO",
        )
        for source, target in fstab_records:
            self.log(f"Matched fstab record for selected disk: {source} -> {target}", "INFO")

        if self.systemd:
            # Stop autofs triggers first so touching a target cannot remount the
            # filesystem while it is being dismantled. Then stop the .mount.
            for target in targets:
                self.run_best_effort(
                    f"Stop systemd automount for {target}",
                    ["systemctl", "stop", unit_name_for_path(target, "automount")],
                )
            for target in targets:
                self.run_best_effort(
                    f"Stop systemd mount for {target}",
                    ["systemctl", "stop", unit_name_for_path(target, "mount")],
                )

        for target in targets:
            self.run_best_effort(f"Unmount {target} recursively", ["umount", "--recursive", "--", target])

        # A partition used as swap also prevents BLKRRPART. swapoff is part of
        # util-linux on the supported distributions, but remains best-effort.
        try:
            swap_lines = Path("/proc/swaps").read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        except OSError:
            swap_lines = []
        device_set = set(device_paths)
        for line in swap_lines:
            fields = line.split()
            if fields and fields[0].startswith("/dev/") and os.path.realpath(fields[0]) in device_set:
                self.run_best_effort(f"Disable swap on {fields[0]}", ["swapoff", fields[0]])

        try:
            os.sync()
        except AttributeError:
            pass
        self.run_best_effort(f"Flush buffered writes for {disk_path}", ["blockdev", "--flushbufs", disk_path])
        if shutil.which("udevadm"):
            self.run_best_effort("Wait for pending udev events", ["udevadm", "settle", "--timeout=10"])

        verification_paths = list(dict.fromkeys([*device_paths, *block_tree_paths(disk_path), *direct_partition_paths(disk_path)]))
        remaining_targets = live_mount_targets_for_devices(verification_paths)
        remaining_swaps: list[str] = []
        try:
            verification_set = set(verification_paths)
            for line in Path("/proc/swaps").read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
                fields = line.split()
                if fields and fields[0].startswith("/dev/") and os.path.realpath(fields[0]) in verification_set:
                    remaining_swaps.append(fields[0])
        except OSError:
            pass

        holders: list[str] = []
        for path in verification_paths:
            name = Path(path).name
            holder_dir = Path("/sys/class/block") / name / "holders"
            try:
                holders.extend(f"{path}->{entry.name}" for entry in holder_dir.iterdir())
            except OSError:
                pass

        if remaining_targets or remaining_swaps or holders:
            diagnostics = [
                f"Mounted targets: {', '.join(remaining_targets) or 'none'}",
                f"Swap devices: {', '.join(remaining_swaps) or 'none'}",
                f"Kernel holders: {', '.join(holders) or 'none'}",
            ]
            if shutil.which("fuser"):
                fuser = command_output(["fuser", "-vm", disk_path], check=False).strip()
                if fuser:
                    diagnostics.append("Open users reported by fuser:\n" + fuser)
            raise StorageError(
                f"{disk_path} is still in use after stopping its mounts and automounts.\n\n"
                + "\n".join(diagnostics)
                + "\n\nClose shells or applications using the mount and retry."
            )

        self.log(f"Selected disk {disk_path} is no longer mounted, swapped, or held by a mapped device.", "OK")
        return list(dict.fromkeys(fstab_sources)), partitions

    def run_parted_verified(
        self,
        title: str,
        name: str,
        command: Sequence[str],
        state_is_committed: Callable[[], bool],
    ) -> None:
        """Run parted and accept a nonzero exit only when disk state proves commit.

        GNU parted may return exit status 1 after writing metadata when the
        kernel rejects BLKRRPART.  Treating that as a hard failure loses the
        useful on-disk result.  We verify the requested state independently and
        then continue into the explicit reread/recovery path.
        """

        output: list[str] = []
        self.status = name
        self.log(f"START: {name}: {shlex.join(command)}")
        self.operation_screen(title, [name], 0, output)
        process = subprocess.Popen(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            clean = line.rstrip()
            if clean:
                output.append(clean)
                self.log(clean, "CMD")
                self.operation_screen(title, [name], 0, output)
        returncode = process.wait()
        committed = state_is_committed()
        if returncode != 0 and not committed:
            self.log(f"FAILED ({returncode}): {name}; requested disk state was not committed.", "ERROR")
            raise StorageError(f"{name} failed with exit code {returncode}.\n\n" + "\n".join(output[-20:]))
        if returncode != 0 and committed:
            self.log(
                f"{name} returned {returncode}, but the requested on-disk state is present; continuing with kernel rediscovery.",
                "WARN",
            )
        else:
            self.log(f"DONE: {name}", "OK")
        self.operation_screen(title, [name], 1, output)
        time.sleep(0.3)

    def assert_selected_disk_still_safe(self, disk: Disk) -> None:
        fresh = {item.node.path: item for item in discover_disks()}
        candidate = fresh.get(disk.node.path)
        if not candidate:
            raise StorageError(
                f"{disk.node.path} is no longer an eligible non-system disk. The operation was stopped before modification."
            )
        expected_serial = disk.node.serial
        if expected_serial and candidate.node.serial and expected_serial != candidate.node.serial:
            raise StorageError(
                f"Device identity changed: expected serial {expected_serial}, now {candidate.node.serial}. Operation stopped."
            )

    def mount_existing(self) -> None:
        disk = self.selected_disk()
        if not disk:
            return
        partitions = [part for part in disk.partitions if part.fstype and part.uuid]
        if not partitions:
            self.modal_box(
                "No mountable filesystem",
                [f"No partition on {disk.node.path} has both a recognized filesystem and UUID.", "Use Format ext4 to create a new filesystem."],
                kind="warning",
            )
            return
        items = [
            (
                part.path,
                f"{human_size(part.size)} | {part.fstype} | label={part.label or '-'} | mounted={part.mountpoint or 'no'}",
            )
            for part in partitions
        ]
        device = self.select_dialog("Select existing filesystem", items)
        if not device:
            return
        part = next(item for item in partitions if item.path == device)
        default_name = re.sub(r"[^A-Za-z0-9._-]", "-", part.label or Path(part.path).name)
        mountpoint = self.input_dialog(
            "Permanent mount point",
            ["Enter the directory where this filesystem should be attached permanently.", "Allowed: /mnt/<name>, /srv/<name>, or /media/<name>."],
            f"/mnt/{default_name}",
            valid_mountpoint,
            "Use a simple path such as /mnt/archive, /srv/storage, or /media/backup.",
        )
        if not mountpoint:
            return
        summary = [
            f"Device: {part.path}",
            f"Filesystem: {part.fstype}",
            f"UUID: {part.uuid}",
            f"Permanent target: {mountpoint}",
            "Existing data will be preserved. /etc/fstab will be backed up before it is changed.",
        ]
        if not self.confirm("Confirm persistent mount", summary):
            return
        try:
            self.assert_selected_disk_still_safe(disk)
            if part.mountpoint:
                self.run_steps(
                    "Preparing existing filesystem",
                    [(f"Unmount {part.mountpoint} so the persistent configuration can take ownership", ["umount", "--", part.mountpoint])],
                )
            fstype, options, passno = filesystem_mount_settings(part.path, self.uid, self.gid, self.systemd)
            backup = self.write_fstab(part.path, mountpoint, fstype, options, passno)
            self.activate_mount(part.path, mountpoint, options)
            if fstype in {"ext2", "ext3", "ext4", "xfs", "btrfs"}:
                if self.confirm("Filesystem ownership", [f"Set the filesystem root directory owner to {self.user}?", "Choose No if existing POSIX ownership must be preserved."]):
                    os.chown(mountpoint, self.uid, self.gid)
                    os.chmod(mountpoint, 0o755)
                    self.log(f"Changed {mountpoint} owner to {self.uid}:{self.gid}.", "OK")
            self.modal_box(
                "Persistent mount configured",
                [f"{part.path} is mounted at {mountpoint}.", f"Filesystem UUID: {part.uuid}", f"fstab backup: {backup}", f"Log: {LOG_FILE}"],
                kind="success",
            )
            self.refresh_devices()
        except StorageError as exc:
            self.log(str(exc), "ERROR")
            self.modal_box("Operation failed", str(exc).splitlines(), kind="error")

    def format_ext4(self) -> None:
        disk = self.selected_disk()
        if not disk:
            return
        label = self.input_dialog(
            "New ext4 label",
            ["Enter a filesystem label (1–16 characters).", "Allowed characters: letters, numbers, dot, underscore, hyphen."],
            "external-data",
            valid_ext4_label,
            "The ext4 label must contain 1–16 letters, numbers, dots, underscores, or hyphens.",
        )
        if not label:
            return
        mountpoint = self.input_dialog(
            "Permanent mount point",
            ["Enter the permanent directory for the new filesystem.", "Allowed: /mnt/<name>, /srv/<name>, or /media/<name>."],
            f"/mnt/{label}",
            valid_mountpoint,
            "Use a simple path such as /mnt/archive, /srv/storage, or /media/backup.",
        )
        if not mountpoint:
            return
        token_identity = disk.node.serial or disk.node.path
        token = f"ERASE {token_identity}"
        typed = self.input_dialog(
            "DESTRUCTIVE CONFIRMATION",
            [
                "THIS WILL DESTROY THE PARTITION TABLE AND EVERY FILESYSTEM ON THE SELECTED DISK.",
                f"Device: {disk.node.path}",
                f"Model: {disk.node.model or 'unknown'}",
                f"Serial: {disk.node.serial or 'not reported'}",
                f"Capacity: {human_size(disk.node.size)}",
                f"Type exactly: {token}",
            ],
            "",
            lambda value: value == token,
            f"Confirmation did not match. Type exactly: {token}",
        )
        if typed is None:
            return
        if not self.confirm(
            "FINAL DESTRUCTIVE WARNING",
            ["All data on the selected physical disk will be lost.", "This is filesystem formatting, not a firmware secure erase.", "Continue?"],
            destructive=True,
        ):
            return
        try:
            self.assert_selected_disk_still_safe(disk)
            stale_fstab_sources, existing_partitions = self.prepare_disk_for_repartition(disk)

            wipe_steps: list[tuple[str, Sequence[str]]] = []
            for partition in reversed(existing_partitions):
                if is_block_device(partition):
                    wipe_steps.append(
                        (f"Remove signatures from {partition}", ["wipefs", "--all", "--force", partition])
                    )
            wipe_steps.append(
                (f"Remove disk signatures from {disk.node.path}", ["wipefs", "--all", "--force", disk.node.path])
            )
            self.run_steps("Clearing existing storage metadata", wipe_steps)

            self.run_parted_verified(
                "Creating a new GPT layout",
                "Create GPT partition table",
                ["parted", "--script", disk.node.path, "mklabel", "gpt"],
                lambda: ":gpt:" in command_output(
                    ["parted", "--machine", "--script", disk.node.path, "unit", "s", "print"],
                    check=False,
                ),
            )
            self.run_parted_verified(
                "Creating a new GPT layout",
                "Create one aligned full-size partition",
                ["parted", "--script", disk.node.path, "mkpart", "primary", "ext4", "1MiB", "100%"],
                lambda: partition_table_has_partition(disk.node.path, 1),
            )
            self.run_best_effort(
                "Ask kernel to reload the new partition table",
                ["partprobe", disk.node.path],
            )
            partition_path = self.wait_for_partition_node(disk.node.path, timeout=60.0)
            self.run_steps(
                "Creating ext4 filesystem",
                [(f"Format {partition_path} as ext4", ["mkfs.ext4", "-F", "-L", label, "-m", "0", partition_path])],
            )
            uuid = command_output(["blkid", "-s", "UUID", "-o", "value", partition_path]).strip()
            if not uuid:
                raise StorageError(f"No UUID was generated for {partition_path}.")
            options = "defaults,nofail,noatime"
            if self.systemd:
                options = "defaults,nofail,x-systemd.automount,x-systemd.device-timeout=10s,noatime"
            backup = self.write_fstab(
                partition_path,
                mountpoint,
                "ext4",
                options,
                "2",
                remove_source_specs=stale_fstab_sources,
            )
            self.activate_mount(partition_path, mountpoint, options)
            os.chown(mountpoint, self.uid, self.gid)
            os.chmod(mountpoint, 0o755)
            self.modal_box(
                "Storage configured successfully",
                [
                    f"Device: {partition_path}",
                    f"Filesystem: ext4 ({label})",
                    f"UUID: {uuid}",
                    f"Mounted at: {mountpoint}",
                    f"Owner: {self.user}",
                    f"fstab backup: {backup}",
                ],
                kind="success",
            )
            self.refresh_devices()
        except StorageError as exc:
            self.log(str(exc), "ERROR")
            self.modal_box("Operation failed", str(exc).splitlines(), kind="error")
            self.refresh_devices()

    def write_fstab(
        self,
        device: str,
        mountpoint: str,
        fstype: str,
        options: str,
        passno: str,
        remove_source_specs: Sequence[str] = (),
    ) -> str:
        uuid = command_output(["blkid", "-s", "UUID", "-o", "value", device]).strip()
        if not uuid:
            raise StorageError(f"No filesystem UUID was found on {device}.")
        Path(mountpoint).mkdir(parents=True, exist_ok=True)
        timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = Path(f"{FSTAB}.backup.{timestamp}")
        temp_path: Optional[Path] = None
        try:
            shutil.copy2(FSTAB, backup)
            self.log(f"Backed up {FSTAB} to {backup}.", "OK")
            original = FSTAB.read_text(encoding="utf-8")
            kept: list[str] = []
            for line in original.splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    kept.append(line)
                    continue
                fields = stripped.split()
                if len(fields) >= 2 and (
                    fields[0] == f"UUID={uuid}"
                    or fields[0] in set(remove_source_specs)
                    or fields[1] == mountpoint
                ):
                    self.log(f"Replacing existing fstab record: {line}", "WARN")
                    continue
                kept.append(line)
            entry = f"UUID={uuid} {mountpoint} {fstype} {options} 0 {passno}"
            kept += [f"# Managed by {APP_NAME} v{VERSION}", entry]
            content = "\n".join(kept).rstrip() + "\n"
            fd, raw_path = tempfile.mkstemp(prefix="fstab.", dir=str(FSTAB.parent))
            os.close(fd)
            temp_path = Path(raw_path)
            temp_path.write_text(content, encoding="utf-8")
            os.chmod(temp_path, 0o644)

            # Syntax validation. findmnt can report absent optional devices as
            # unreachable, so the decisive condition is zero parser errors.
            verify = subprocess.run(
                ["findmnt", "--verify", "--verbose", "--tab-file", str(temp_path)],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            verify_text = verify.stdout
            self.log("Validating the generated /etc/fstab configuration.")
            for line in verify_text.splitlines():
                self.log(line, "VERIFY")
            syntax_valid = findmnt_fstab_syntax_valid(verify_text, verify.returncode)
            self.log(
                f"findmnt validation result: returncode={verify.returncode}, syntax_valid={syntax_valid}",
                "VERIFY",
            )
            if not syntax_valid:
                raise StorageError(
                    "The generated fstab failed syntax validation. The original file was not changed.\n"
                    + verify_text[-1500:]
                )
            # Rewrite the existing inode rather than replacing it. This keeps
            # ownership and security labels (notably SELinux contexts) attached
            # to /etc/fstab on distributions that enforce them.
            with FSTAB.open("w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temp_path.unlink(missing_ok=True)
            temp_path = None
            os.chmod(FSTAB, 0o644)
            if shutil.which("restorecon"):
                subprocess.run(["restorecon", str(FSTAB)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.log(f"Wrote persistent UUID mount: {entry}", "OK")
            if self.systemd:
                self.run_steps("Reloading systemd mount configuration", [("Reload systemd units", ["systemctl", "daemon-reload"])])
            return str(backup)
        except Exception:
            if temp_path and temp_path.exists():
                temp_path.unlink(missing_ok=True)
            raise

    def activate_mount(self, device: str, mountpoint: str, options: str) -> None:
        if self.systemd and "x-systemd.automount" in options:
            automount_unit = unit_name_for_path(mountpoint, "automount")
            mount_unit = unit_name_for_path(mountpoint, "mount")
            self.run_steps(
                "Activating systemd automount",
                [
                    (f"Start {automount_unit}", ["systemctl", "start", automount_unit]),
                    # Start the matching .mount unit explicitly. Accessing the
                    # directory normally triggers it too, but an explicit start
                    # removes ambiguity and gives the user a useful error if the
                    # actual filesystem mount fails.
                    (f"Start {mount_unit}", ["systemctl", "start", mount_unit]),
                ],
            )
        else:
            self.run_steps("Mounting filesystem", [(f"Mount {mountpoint} from fstab", ["mount", mountpoint])])

        deadline = time.monotonic() + 15.0
        last_detail = "mount verification has not run"
        while time.monotonic() < deadline:
            mounted, detail = verify_device_mounted_at(device, mountpoint)
            last_detail = detail
            if mounted:
                self.log(f"Verified {device} mounted at {mountpoint}: {detail}.", "OK")
                return
            self.log(f"Waiting for real filesystem mount; observed: {detail}", "VERIFY")
            time.sleep(0.5)

        automount_view = command_output(
            ["findmnt", "-r", "-n", "--mountpoint", mountpoint, "-o", "SOURCE,FSTYPE,TARGET"],
            check=False,
        ).strip()
        raise StorageError(
            f"The fstab entry was written, but the real filesystem {device} was not verified at {mountpoint}.\n"
            f"Observed exact-target mounts: {last_detail}.\n"
            f"findmnt view: {automount_view or 'none'}.\n"
            "A source of systemd-1 with type autofs is only the on-demand trigger, not the backing filesystem."
        )


def install_self() -> None:
    report = dependency_report()
    if not report.ok:
        print_dependency_failure(report)
        raise SystemExit(2)
    elevate_if_needed(sys.argv[1:])
    source = Path(__file__).resolve()
    shutil.copy2(source, INSTALL_PATH)
    os.chmod(INSTALL_PATH, 0o755)
    print(f"Installed {APP_NAME} v{VERSION} build {BUILD_ID} to {INSTALL_PATH}")
    print("Run it with: sudo permanent-storage-manager")


def list_devices_cli() -> None:
    disks = discover_disks()
    payload = []
    for disk in disks:
        payload.append(
            {
                "path": disk.node.path,
                "model": disk.node.model,
                "serial": disk.node.serial,
                "size_bytes": disk.node.size,
                "classification": disk.external_reason,
                "partitions": [part.__dict__ for part in disk.partitions],
            }
        )
    print(json.dumps(payload, indent=2))


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION} (build {BUILD_ID})")
    parser.add_argument("--check-deps", action="store_true", help="check dependencies and print an install command when needed")
    parser.add_argument("--list-devices", action="store_true", help="print eligible non-system disks as JSON and exit")
    parser.add_argument("--install", action="store_true", help=f"install this program to {INSTALL_PATH}")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    report = dependency_report()
    if args.check_deps:
        if report.ok:
            print("All required dependencies are installed.")
            return 0
        print_dependency_failure(report)
        return 2
    if args.install:
        install_self()
        return 0
    if not report.ok:
        print_dependency_failure(report)
        return 2
    if args.list_devices:
        list_devices_cli()
        return 0

    elevate_if_needed(sys.argv[1:])
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("This application requires an interactive terminal.", file=sys.stderr)
        return 2

    import curses

    try:
        curses.wrapper(lambda stdscr: TuiApp(stdscr).run())
    except StorageError as exc:
        print(f"{APP_NAME}: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

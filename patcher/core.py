"""Image patching core: mounting, individual patches, and orchestration.

The patcher operates on a working copy of the image. Each patch is a small
function that takes a mount point + options and mutates files inside it. The
orchestrator wires patches up to options, mounts each partition once, runs the
patches that apply, and unmounts. Mounts are tracked in a registry so that
even on hard failure the cleanup path can unmount everything (unless
`--no-cleanup-on-error` is set, in which case mounts are left in place for
inspection).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import dashboard


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
RESOURCES = PACKAGE_ROOT / "resources"
PROJECT_ROOT = PACKAGE_ROOT.parent

# Resource files copied verbatim into the rootfs.
RES_UDEV_CAN = RESOURCES / "90-usb-can-rename.rules"
RES_CANBUSPROCESS = RESOURCES / "canbusprocess-override.conf"
RES_CANBUSWATCHDOG = RESOURCES / "canbuswatchdog-override.conf"
RES_ROBOT = RESOURCES / "robot-override.conf"
RES_PICOFLASHER = RESOURCES / "picoflasher-override.conf"
RES_MRCCAN = RESOURCES / "mrccan.conf"
RES_MODULES_LOAD = RESOURCES / "modules-load.conf"
RES_CAMERA_SHIM_SO = RESOURCES / "camera-shim" / "pi5b-camera-shim.so"
RES_CAMERA_SHIM_CONF = RESOURCES / "camera-shim" / "visionserver-camera-shim.conf"

# The four vision server instances that get the camera-shim drop-in.
VISIONSERVER_UNITS = ("limelight_visionserver", "limelight_visionserver1",
                      "limelight_visionserver2", "limelight_visionserver3")
RES_ALPHA2_PI5B_DTB = RESOURCES / "alpha2" / "bcm2712-rpi-5-b.dtb"
RES_ALPHA2_PI5B_DTB_D0 = RESOURCES / "alpha2" / "bcm2712d0-rpi-5-b.dtb"

# Project-relative defaults the user can override.
DEFAULT_FLASH_PICO = PROJECT_ROOT / "netboot" / "flash-pico.sh"
DEFAULT_REGDB_DEB = (
    PROJECT_ROOT / "beta9" / "wireless-regdb_2025.10.07-0ubuntu1~24.04.1_all.deb"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ImageType(Enum):
    """Upstream image families, distinguished by partition layout.

    The A/B layout covers Beta 10 through the current 2027.0.0 betas *and*
    Alpha 11+ — since Alpha 11 the alpha line ships the same partition table
    as the beta line, so both are patched identically.  Only Alpha 2–10 use
    the older single-boot-partition layout.
    """

    AB = "ab"           # boot selector + boot A/B + rootfs A/B
    ALPHA = "alpha"     # 4 partitions: single boot + rootfs_a/b + data
    UNKNOWN = "unknown"


# Accepted --image-type values, including names of superseded releases.
IMAGE_TYPE_ALIASES: dict[str, ImageType] = {
    "ab": ImageType.AB,
    "beta": ImageType.AB,
    "beta10": ImageType.AB,   # what this type was called before Alpha 11
    "alpha": ImageType.ALPHA,
}


class PatcherError(Exception):
    """A patch step failed in a way that should abort the run."""


class PreflightError(PatcherError):
    """A pre-run check failed — typically missing tools, missing root, or
    a malformed source image."""


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class PatchOptions:
    """All knobs exposed by the GUI and CLI.

    Patch toggles default to True so the default behavior matches what
    `build-image.sh` does. Set any to False to skip that patch.
    """

    # --- I/O ---
    input_image: Path = Path("")
    output_image: Path = Path("")
    flash_pico_path: Path = DEFAULT_FLASH_PICO
    regdb_deb_path: Path = DEFAULT_REGDB_DEB

    # --- Boot partition patches (shared) ---
    enable_hdmi: bool = True
    disable_spi_can: bool = True
    update_cmdline: bool = True

    # --- Boot partition patches (legacy Alpha 2-10 only, skipped on A/B images) ---
    fix_autoboot: bool = True
    fix_config_sections: bool = True
    fix_cmdline_reference: bool = True
    install_pi5b_dtb: bool = True
    comment_pi4_firmware: bool = True

    # --- Rootfs patches ---
    install_flash_pico: bool = True
    install_can_udev: bool = True
    install_canbusprocess: bool = True
    install_canbuswatchdog: bool = True
    install_robot_override: bool = True
    install_mrccan: bool = True
    install_modules_load: bool = True
    install_camera_shim: bool = True
    install_regdb: bool = True
    patch_dashboard_wlan: bool = True
    patch_dashboard_faults: bool = True

    # --- Operational flags ---
    force_image_type: Optional[ImageType] = None
    dry_run: bool = False
    verbose: bool = False
    backup: bool = False
    keep_mounted: bool = False
    cleanup_on_error: bool = True
    validate_after: bool = False
    skip_b_partitions: bool = False  # only patch A, leave B alone

    def patch_names(self) -> list[str]:
        """Names of every patch toggle, used for --list-patches and the GUI."""
        return [
            "enable_hdmi",
            "disable_spi_can",
            "update_cmdline",
            "fix_autoboot",
            "fix_config_sections",
            "fix_cmdline_reference",
            "install_pi5b_dtb",
            "comment_pi4_firmware",
            "install_flash_pico",
            "install_can_udev",
            "install_canbusprocess",
            "install_canbuswatchdog",
            "install_robot_override",
            "install_mrccan",
            "install_modules_load",
            "install_camera_shim",
            "install_regdb",
            "patch_dashboard_wlan",
            "patch_dashboard_faults",
        ]


PATCH_DESCRIPTIONS: dict[str, str] = {
    "enable_hdmi": "Uncomment HDMI display options in config.txt",
    "disable_spi_can": "Comment out spi/sc-mcp2518 overlays (no SPI CAN on Pi 5B)",
    "update_cmdline": "Add panic=0 and cfg80211.ieee80211_regdom=US to cmdline.txt",
    "fix_autoboot": "Disable autoboot.txt partition redirection (Alpha 2-10)",
    "fix_config_sections": "Add [all] before global settings in config.txt (Alpha 2-10)",
    "fix_cmdline_reference": "Create cmdline.txt from cmdline_a.txt (Alpha 2-10)",
    "install_pi5b_dtb": "Install bcm2712-rpi-5-b.dtb for Pi 5 Model B (Alpha 2-10)",
    "comment_pi4_firmware": "Comment out Pi 4 firmware references in config.txt (Alpha 2-10)",
    "install_flash_pico": "Install flash-pico.sh + picoflasherprocess override",
    "install_can_udev": "Install 90-usb-can-rename.rules (USB-only trigger)",
    "install_canbusprocess": "Install canbusprocess override (vcan placeholders + heartbeat)",
    "install_canbuswatchdog": "Install canbuswatchdog override (waits for any can_s*)",
    "install_robot_override": "Install robot.service override (30s wait for can_s0..s4)",
    "install_mrccan": "Install /etc/tmpfiles.d/mrccan.conf (MrcCommDaemon fallback)",
    "install_modules_load": "Load i2c-dev at boot (robot_heartbeat comes from canbusprocess)",
    "install_camera_shim": "Let vision servers use a camera plugged straight into a Pi 5B port",
    "install_regdb": "Install wireless-regdb so WiFi works on US regulatory domain",
    "patch_dashboard_wlan": "Unlock WLAN0 AP settings in the dashboard JS",
    "patch_dashboard_faults": "Add a 'Reset Fault Counts' button to the dashboard",
}


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def run(
    cmd: list[str],
    log: logging.Logger,
    check: bool = True,
    capture: bool = False,
    dry_run: bool = False,
) -> subprocess.CompletedProcess:
    """Run a subprocess command with consistent logging.

    `dry_run=True` only logs the command and returns a fake CompletedProcess —
    use this for any command that mutates state. Read-only commands (sfdisk,
    file, parted) should pass dry_run=False even in dry-run mode so the run
    can still inspect the image.
    """
    log.debug("$ %s", " ".join(str(c) for c in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, b"", b"")
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=capture,
    )


def run_text(cmd: list[str], log: logging.Logger) -> str:
    """Run a read-only command and return stdout as text."""
    res = run(cmd, log, capture=True)
    return res.stdout


# ---------------------------------------------------------------------------
# Partition discovery
# ---------------------------------------------------------------------------


@dataclass
class Partition:
    index: int          # 1-based partition number
    start_bytes: int    # byte offset of partition start within the image
    size_bytes: int     # partition size in bytes
    fs: str             # detected filesystem ("vfat", "ext4", "extended", "unknown")
    present: bool = True  # False when the partition extends past end-of-file

    @property
    def end_bytes(self) -> int:
        return self.start_bytes + self.size_bytes


@dataclass
class ImageLayout:
    """Identified partitions of interest inside an image file."""

    image: Path
    partitions: list[Partition]
    image_type: ImageType = ImageType.UNKNOWN
    boot_a: Optional[Partition] = None
    boot_b: Optional[Partition] = None
    root_a: Optional[Partition] = None
    root_b: Optional[Partition] = None
    # True when the image is shipped "packed": the partition table declares a
    # rootfs B that isn't in the file. /usr/local/bin/expandfs.sh grows the
    # table and formats slot B on first boot (Beta 14 / Alpha 14 and later).
    packed: bool = False


def detect_layout(image: Path, log: logging.Logger) -> ImageLayout:
    """Detect partition offsets and identify the boot/root A/B partitions.

    A/B layout (Beta 10+, Alpha 11+): boot selector + boot A/B + extended +
    rootfs A/B.  Two FAT32 boot partitions >= 30 MB.

    Alpha layout (Alpha 2-10): single boot + rootfs_a/b + data.  One FAT32
    boot partition, no separate boot A/B.

    Since Beta 14 / Alpha 14 the images ship *packed*: rootfs A is shrunk to
    its filesystem size and rootfs B is declared in the partition table but
    lies past the end of the file (first boot runs expandfs.sh, which grows
    both slots to 7 GiB and formats B).  Partitions that don't fit inside the
    file are marked ``present=False`` and are never mounted.

    Partitions are identified by size and filesystem rather than by index so
    the patcher survives layout changes in future upstream releases.
    """
    log.info("Detecting partition layout of %s", image)

    dump = run_text(["sfdisk", "-d", str(image)], log)
    image_size = image.stat().st_size

    partitions: list[Partition] = []
    line_re = re.compile(
        r"^\S+\s*:\s*start=\s*(\d+),\s*size=\s*(\d+),.*type=([0-9a-fA-Fx]+)"
    )
    for line in dump.splitlines():
        m = line_re.match(line)
        if not m:
            continue
        start_sec, size_sec, ptype = m.groups()
        start = int(start_sec) * 512
        size = int(size_sec) * 512
        # A packed image declares partitions that aren't in the file yet;
        # mounting one would fail, so record it and move on. The extended
        # container legitimately spans the not-yet-created partitions, so it
        # is judged by its start sector alone.
        is_extended = ptype.lower() in ("5", "0x5", "f", "0xf")
        present = start < image_size if is_extended else start + size <= image_size
        fs = _detect_fs(image, start, ptype, log) if present else "absent"
        if not present:
            if start < image_size:
                # The file ends *inside* this partition. A packed image starts
                # its missing partitions cleanly past EOF, so this is a
                # truncated or partially-copied file, not a packed one.
                log.warning(
                    "Partition %d (offset %d, %.1f MB) is cut off by the end of "
                    "the file — the image looks truncated or incompletely "
                    "downloaded. Skipping it; re-download if that's unexpected.",
                    len(partitions) + 1, start, size / (1024 * 1024),
                )
            else:
                log.info(
                    "Partition %d (offset %d, %.1f MB) lies past end of file — "
                    "not present in this image",
                    len(partitions) + 1, start, size / (1024 * 1024),
                )
        partitions.append(
            Partition(index=len(partitions) + 1, start_bytes=start,
                      size_bytes=size, fs=fs, present=present)
        )

    if not partitions:
        raise PreflightError(f"sfdisk found no partitions in {image}")

    fat_parts = [p for p in partitions
                 if p.present and p.fs == "vfat" and p.size_bytes < 200 * 1024 * 1024]
    ext_parts = [p for p in partitions if p.present and p.fs == "ext4"]

    fat_parts.sort(key=lambda p: p.start_bytes)
    ext_parts.sort(key=lambda p: p.start_bytes)

    boot_candidates = [p for p in fat_parts if p.size_bytes >= 30 * 1024 * 1024]

    layout = ImageLayout(image=image, partitions=partitions)

    if len(boot_candidates) >= 2:
        layout.image_type = ImageType.AB
        layout.boot_a = boot_candidates[0]
        layout.boot_b = boot_candidates[1]
    elif len(boot_candidates) == 1:
        layout.image_type = ImageType.ALPHA
        layout.boot_a = boot_candidates[0]
    else:
        layout.image_type = ImageType.UNKNOWN

    if len(ext_parts) >= 1:
        layout.root_a = ext_parts[0]
    if len(ext_parts) >= 2:
        layout.root_b = ext_parts[1]

    # A/B image with only one usable rootfs = packed image; slot B is created
    # on first boot by expandfs.sh and there is nothing there to patch.
    layout.packed = (
        layout.image_type == ImageType.AB
        and layout.root_a is not None
        and layout.root_b is None
    )
    if layout.packed:
        log.info(
            "Packed image: rootfs B is not in the file (created on first boot "
            "by expandfs.sh). Patching rootfs A only — an OTA update to slot B "
            "overwrites it wholesale and would drop these patches anyway."
        )

    log.info("Detected image type: %s", layout.image_type.value)
    for label, part in [
        ("boot_a", layout.boot_a),
        ("boot_b", layout.boot_b),
        ("root_a", layout.root_a),
        ("root_b", layout.root_b),
    ]:
        if part:
            log.info(
                "  %s -> partition %d, %s, offset=%d, size=%.1f MB",
                label,
                part.index,
                part.fs,
                part.start_bytes,
                part.size_bytes / (1024 * 1024),
            )
        else:
            log.debug("  %s -> not present", label)

    return layout


def _detect_fs(image: Path, offset: int, ptype: str, log: logging.Logger) -> str:
    """Identify the filesystem at a given offset by its superblock magic.

    Reading the magic bytes directly avoids both a loop device and a
    dependency on the `file` binary.
    """
    if ptype.lower() in ("5", "0x5", "f", "0xf"):
        # Extended partition container; no real filesystem.
        return "extended"

    # Peek at the first 8KB — enough for superblocks of common filesystems.
    with open(image, "rb") as f:
        f.seek(offset)
        head = f.read(8192)

    # ext2/3/4 magic: 0xEF53 at offset 0x438 from start of filesystem.
    if len(head) >= 0x43A and head[0x438:0x43A] == b"\x53\xef":
        return "ext4"
    # FAT: look for "MSWIN", "FAT16", "FAT32", or the OEM name at offset 3.
    if b"FAT32" in head[:90] or b"FAT16" in head[:90] or b"mkfs.fat" in head[:90]:
        return "vfat"
    if head[510:512] == b"\x55\xaa":
        # Has a boot signature but no recognized FS — might still be FAT
        # (some images don't write the FAT32 string in the OEM field).
        return "vfat"

    return "unknown"


# ---------------------------------------------------------------------------
# Mount tracking
# ---------------------------------------------------------------------------


@dataclass
class MountTracker:
    """Tracks active mounts so they can be torn down in LIFO order on cleanup."""

    mounts: list[Path] = field(default_factory=list)

    def push(self, path: Path) -> None:
        self.mounts.append(path)

    def cleanup(self, log: logging.Logger) -> None:
        # Unmount in reverse order in case of nested mounts.
        while self.mounts:
            mnt = self.mounts.pop()
            try:
                run(["umount", str(mnt)], log)
                mnt.rmdir()
            except subprocess.CalledProcessError:
                log.warning("Failed to unmount %s (already gone?)", mnt)
            except OSError:
                log.warning("Mount point %s couldn't be removed", mnt)


@contextmanager
def mount_partition(
    image: Path, part: Partition, log: logging.Logger, tracker: MountTracker
) -> Iterator[Path]:
    """Loop-mount a partition at `part.start_bytes` and yield the mount point.

    The mount point is registered with `tracker` so a top-level finally block
    can guarantee cleanup even when an inner patch raises. If the mount call
    itself fails, the temp directory is removed before re-raising so we don't
    leak `/tmp/patcher-mnt-*` directories.
    """
    mnt = Path(tempfile.mkdtemp(prefix="patcher-mnt-"))
    log.debug("Mounting partition %d at %s", part.index, mnt)
    try:
        run(
            [
                "mount",
                "-o",
                f"loop,offset={part.start_bytes},sizelimit={part.size_bytes}",
                str(image),
                str(mnt),
            ],
            log,
        )
    except Exception:
        mnt.rmdir()
        raise
    tracker.push(mnt)
    # Caller may want to keep mounts open (--keep-mounted). The tracker is
    # the source of truth — if we removed `mnt` from it the cleanup phase
    # will skip it, so the with-block exit doesn't need to unmount here.
    yield mnt


# ---------------------------------------------------------------------------
# File mutation helpers
# ---------------------------------------------------------------------------


def sed_inplace(path: Path, pattern: str, replacement: str, log: logging.Logger,
                dry_run: bool = False) -> int:
    """Apply a single regex substitution to a file in-place. Returns
    the number of substitutions made. Uses re.MULTILINE so `^` and `$` match
    line boundaries the same way `sed -i 's/^.../.../'` does.
    """
    if not path.exists():
        log.warning("sed: %s does not exist, skipping", path)
        return 0
    original = path.read_text(encoding="utf-8", errors="surrogateescape")
    patched, count = re.subn(pattern, replacement, original, flags=re.MULTILINE)
    if count == 0:
        log.debug("sed: no matches for %r in %s", pattern, path.name)
        return 0
    log.info("sed: %d substitution(s) in %s", count, path.name)
    if not dry_run:
        path.write_text(patched, encoding="utf-8", errors="surrogateescape")
    return count


def copy_into(src: Path, dst: Path, log: logging.Logger, dry_run: bool = False,
              mode: Optional[int] = None) -> None:
    """Copy `src` to `dst`, creating parents as needed."""
    log.info("Installing %s -> %s", src.name, dst)
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if mode is not None:
        dst.chmod(mode)


# ---------------------------------------------------------------------------
# Boot partition patches
# ---------------------------------------------------------------------------


def patch_boot_partition(mount: Path, opts: PatchOptions, log: logging.Logger,
                          label: str) -> None:
    """Apply all enabled boot-partition patches."""
    log.info("=== Patching boot partition %s ===", label)

    config = mount / "config.txt"
    cmdline = mount / "cmdline.txt"

    if opts.enable_hdmi:
        log.info("[%s] Enabling HDMI", label)
        for pat, repl in [
            (r"^hdmi_ignore_hotplug=1", "#hdmi_ignore_hotplug=1"),
            (r"^hdmi_ignore_edid=0xa5000080", "#hdmi_ignore_edid=0xa5000080"),
            (r"^hdmi_blanking=2", "#hdmi_blanking=2"),
            (r"^ignore_lcd=1", "#ignore_lcd=1"),
            (r"^display_auto_detect=0", "display_auto_detect=1"),
        ]:
            sed_inplace(config, pat, repl, log, opts.dry_run)

    if opts.disable_spi_can:
        log.info("[%s] Commenting out SPI CAN overlays", label)
        sed_inplace(config, r"^(dtoverlay=spi[0-9].*)$", r"#\1", log, opts.dry_run)
        sed_inplace(config, r"^(dtoverlay=sc-mcp2518.*)$", r"#\1", log, opts.dry_run)

    if opts.update_cmdline and cmdline.exists():
        content = cmdline.read_text(encoding="utf-8").rstrip("\n")
        changed = False
        if "panic=" not in content:
            content += " panic=0"
            changed = True
        if "cfg80211" not in content:
            content += " cfg80211.ieee80211_regdom=US"
            changed = True
        if changed:
            log.info("[%s] Updating cmdline.txt", label)
            if not opts.dry_run:
                cmdline.write_text(content + "\n")


def patch_boot_alpha(mount: Path, opts: PatchOptions, log: logging.Logger,
                     label: str) -> None:
    """Apply legacy-Alpha boot fixes, then shared boot patches.

    Alpha 2-10 images have a single boot partition with autoboot.txt that redirects
    to an ext4 rootfs partition (unreadable by the Pi 5 bootloader), config.txt
    conditional sections that hide ``kernel=Image`` from normal boot, and no
    Pi 5 Model B device tree.
    """
    log.info("=== Patching boot partition %s (Alpha) ===", label)

    config = mount / "config.txt"
    autoboot = mount / "autoboot.txt"

    if opts.fix_autoboot and autoboot.exists():
        log.info("[%s] Disabling autoboot.txt partition redirection", label)
        if not opts.dry_run:
            autoboot.rename(mount / "autoboot.txt.disabled")

    if opts.fix_config_sections and config.exists():
        _fix_alpha_config_sections(config, log, opts.dry_run)

    if opts.fix_cmdline_reference:
        cmdline = mount / "cmdline.txt"
        cmdline_a = mount / "cmdline_a.txt"
        if not cmdline.exists() and cmdline_a.exists():
            log.info("[%s] Creating cmdline.txt from cmdline_a.txt", label)
            if not opts.dry_run:
                shutil.copy2(cmdline_a, cmdline)

    if opts.install_pi5b_dtb:
        if RES_ALPHA2_PI5B_DTB.exists():
            copy_into(RES_ALPHA2_PI5B_DTB, mount / "bcm2712-rpi-5-b.dtb",
                      log, opts.dry_run)
        else:
            log.warning("[%s] Pi 5B DTB not found at %s", label, RES_ALPHA2_PI5B_DTB)
        if RES_ALPHA2_PI5B_DTB_D0.exists():
            copy_into(RES_ALPHA2_PI5B_DTB_D0, mount / "bcm2712d0-rpi-5-b.dtb",
                      log, opts.dry_run)

    if opts.comment_pi4_firmware and config.exists():
        log.info("[%s] Commenting out Pi 4 firmware references", label)
        sed_inplace(config, r"^(start_file=.*)$", r"#\1", log, opts.dry_run)
        sed_inplace(config, r"^(fixup_file=.*)$", r"#\1", log, opts.dry_run)

    patch_boot_partition(mount, opts, log, label)


def _fix_alpha_config_sections(config: Path, log: logging.Logger,
                               dry_run: bool) -> None:
    """Insert ``[all]`` before global settings in Alpha config.txt.

    Alpha config.txt has ``[boot_partition=2]`` and ``[boot_partition=3]``
    sections at the top.  Everything after the last conditional — including
    ``kernel=Image`` — is trapped inside that section and invisible when
    booting from partition 1.  Inserting ``[all]`` lifts the global settings
    out of the conditional block.
    """
    text = config.read_text(encoding="utf-8", errors="surrogateescape")
    if "[all]" in text:
        log.debug("config.txt already has [all] section")
        return
    patched, n = re.subn(
        r"^(\[boot_partition=3\]\s*\n(?:cmdline=\S+\n))\n",
        r"\1\n[all]\n",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        patched, n = re.subn(
            r"^(kernel=Image)",
            r"[all]\n\1",
            text,
            count=1,
            flags=re.MULTILINE,
        )
    if n == 0:
        log.warning("Could not insert [all] — config.txt structure not recognized")
        return
    log.info("Inserted [all] section in config.txt")
    if not dry_run:
        config.write_text(patched, encoding="utf-8", errors="surrogateescape")


# ---------------------------------------------------------------------------
# Rootfs partition patches
# ---------------------------------------------------------------------------


def patch_rootfs_partition(mount: Path, opts: PatchOptions, log: logging.Logger,
                           label: str) -> None:
    """Apply all enabled rootfs patches."""
    log.info("=== Patching rootfs %s ===", label)
    log.info("[%s] Upstream release: %s", label, read_os_release(mount))

    if opts.install_flash_pico:
        if not opts.flash_pico_path.exists():
            log.warning("[%s] flash-pico.sh not found at %s, skipping",
                        label, opts.flash_pico_path)
        else:
            dst_script = mount / "usr/local/bin/flash-pico.sh"
            copy_into(opts.flash_pico_path, dst_script, log, opts.dry_run, mode=0o755)
            dst_override = (
                mount / "etc/systemd/system/limelight_picoflasherprocess.service.d/override.conf"
            )
            copy_into(RES_PICOFLASHER, dst_override, log, opts.dry_run)

    if opts.install_can_udev:
        copy_into(RES_UDEV_CAN, mount / "etc/udev/rules.d/90-usb-can-rename.rules",
                  log, opts.dry_run)

    if opts.install_canbusprocess:
        copy_into(
            RES_CANBUSPROCESS,
            mount / "etc/systemd/system/limelight_canbusprocess.service.d/override.conf",
            log, opts.dry_run,
        )

    if opts.install_canbuswatchdog:
        copy_into(
            RES_CANBUSWATCHDOG,
            mount / "etc/systemd/system/limelight_canbuswatchdog.service.d/override.conf",
            log, opts.dry_run,
        )

    if opts.install_robot_override:
        copy_into(
            RES_ROBOT,
            mount / "etc/systemd/system/robot.service.d/override.conf",
            log, opts.dry_run,
        )

    if opts.install_mrccan:
        copy_into(RES_MRCCAN, mount / "etc/tmpfiles.d/mrccan.conf", log, opts.dry_run)

    if opts.install_modules_load:
        copy_into(RES_MODULES_LOAD,
                  mount / "etc/modules-load.d/systemcore-pi5b.conf",
                  log, opts.dry_run)

    if opts.install_camera_shim:
        # LD_PRELOAD wrapper around realpath() so the vision servers accept a
        # camera on a bare Pi 5B port (they only match the carrier board's
        # hub path). One drop-in per vision server instance.
        copy_into(RES_CAMERA_SHIM_SO, mount / "usr/local/lib/pi5b-camera-shim.so",
                  log, opts.dry_run, mode=0o644)
        for unit in VISIONSERVER_UNITS:
            copy_into(RES_CAMERA_SHIM_CONF,
                      mount / f"etc/systemd/system/{unit}.service.d/20-pi5b-camera.conf",
                      log, opts.dry_run)

    if opts.install_regdb:
        if (mount / "usr/lib/firmware/regulatory.db").exists():
            log.info("[%s] regulatory.db already present upstream, skipping regdb install",
                     label)
        elif not opts.regdb_deb_path.exists():
            log.warning("[%s] wireless-regdb .deb not found at %s, skipping",
                        label, opts.regdb_deb_path)
        else:
            _install_regdb(mount, opts.regdb_deb_path, log, opts.dry_run, label)

    if opts.patch_dashboard_wlan or opts.patch_dashboard_faults:
        _patch_dashboard(mount, opts, log, label)


def read_os_release(mount: Path) -> str:
    """Return PRETTY_NAME from a mounted rootfs, or "unknown"."""
    os_release = mount / "etc/os-release"
    if not os_release.exists():
        return "unknown"
    for line in os_release.read_text(errors="replace").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return "unknown"


def _install_regdb(mount: Path, deb: Path, log: logging.Logger,
                   dry_run: bool, label: str) -> None:
    log.info("[%s] Installing wireless-regdb from %s", label, deb.name)
    if dry_run:
        return
    with tempfile.TemporaryDirectory(prefix="regdb-") as tmpdir:
        run(["dpkg-deb", "-x", str(deb), tmpdir], log)
        fw_src = Path(tmpdir) / "lib" / "firmware"
        fw_dst = mount / "usr/lib/firmware"
        fw_dst.mkdir(parents=True, exist_ok=True)
        for f in fw_src.iterdir():
            shutil.copy2(f, fw_dst / f.name)


def _patch_dashboard(mount: Path, opts: PatchOptions, log: logging.Logger,
                     label: str) -> None:
    """Apply the dashboard patches to the minified React bundle.

    Every transform lives in `patcher.dashboard`, shared verbatim with the
    Windows patcher, so the two can't drift.
    """
    js = _find_dashboard_js(mount)
    if js is None:
        log.warning("[%s] Dashboard main.*.js not found, skipping dashboard patches",
                    label)
        return
    log.info("[%s] Patching dashboard at %s", label, js.name)

    text = js.read_text(encoding="utf-8", errors="surrogateescape")
    patched = dashboard.apply_patches(
        text, log, label,
        wlan=opts.patch_dashboard_wlan,
        faults=opts.patch_dashboard_faults,
    )
    if patched == text:
        log.info("[%s] Dashboard unchanged", label)
        return
    if not opts.dry_run:
        js.write_text(patched, encoding="utf-8", errors="surrogateescape")


def _find_dashboard_js(mount: Path) -> Optional[Path]:
    """The dashboard bundle inside a mounted rootfs, or None."""
    js_files = sorted((mount / "var/www/html/static/js").glob("main.*.js"))
    return js_files[0] if js_files else None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(layout: ImageLayout, log: logging.Logger) -> list[str]:
    """Mount each rootfs and verify expected post-patch files exist.

    Returns a list of human-readable problems. Empty list = clean validation.
    """
    log.info("=== Validating patched image ===")
    problems: list[str] = []
    checked: list[str] = []
    tracker = MountTracker()
    try:
        if layout.image_type == ImageType.ALPHA and layout.boot_a:
            with mount_partition(layout.image, layout.boot_a, log, tracker) as mnt:
                for name in ("bcm2712-rpi-5-b.dtb", "cmdline.txt", "Image"):
                    if not (mnt / name).exists():
                        problems.append(f"[boot] missing: {name}")
                config = mnt / "config.txt"
                if config.exists() and "[all]" not in config.read_text():
                    problems.append("[boot] config.txt missing [all] section")
                autoboot = mnt / "autoboot.txt"
                if autoboot.exists():
                    problems.append("[boot] autoboot.txt should be disabled")
        for label, part in [("rootfs_a", layout.root_a), ("rootfs_b", layout.root_b)]:
            if part is None:
                continue
            checked.append(label)
            with mount_partition(layout.image, part, log, tracker) as mnt:
                expected = [
                    "etc/tmpfiles.d/mrccan.conf",
                    "etc/modules-load.d/systemcore-pi5b.conf",
                    "usr/local/lib/pi5b-camera-shim.so",
                    "etc/systemd/system/limelight_visionserver.service.d/20-pi5b-camera.conf",
                    "etc/systemd/system/limelight_visionserver3.service.d/20-pi5b-camera.conf",
                    "etc/udev/rules.d/90-usb-can-rename.rules",
                    "etc/systemd/system/limelight_canbusprocess.service.d/override.conf",
                    "etc/systemd/system/robot.service.d/override.conf",
                    "usr/local/bin/flash-pico.sh",
                ]
                for rel in expected:
                    p = mnt / rel
                    if not p.exists():
                        problems.append(f"[{label}] missing: {rel}")
                    else:
                        log.debug("[%s] ok: %s", label, rel)
                problems += _validate_dashboard(mnt, log, label)
    finally:
        tracker.cleanup(log)
    if not problems:
        log.info("Validation passed: all expected files present in %s",
                 " + ".join(checked) if checked else "no rootfs (nothing to check)")
    else:
        for p in problems:
            log.error(p)
    return problems


def _validate_dashboard(mount: Path, log: logging.Logger, label: str) -> list[str]:
    """Check the dashboard bundle actually carries the patches.

    File-existence checks can't see this: the bundle is always present, and a
    sed whose pattern went stale leaves it looking untouched but working.
    """
    js = _find_dashboard_js(mount)
    if js is None:
        return [f"[{label}] dashboard main.*.js not found"]
    text = js.read_text(encoding="utf-8", errors="surrogateescape")
    problems = [f"[{label}] dashboard {js.name}: {p}"
                for p in dashboard.check_patched(text)]
    if not problems:
        log.info("[%s] ok: dashboard %s (AP unlocked, fault reset button, "
                 "fault baseline)", label, js.name)

    node = shutil.which("node")
    if node:
        res = subprocess.run([node, "--check", str(js)],
                             capture_output=True, text=True)
        if res.returncode != 0:
            problems.append(f"[{label}] dashboard {js.name}: node --check failed: "
                            f"{res.stderr.strip().splitlines()[0] if res.stderr.strip() else ''}")
        else:
            log.info("[%s] ok: dashboard %s parses (node --check)", label, js.name)
    else:
        log.info("[%s] node not on PATH, skipping syntax check of %s "
                 "(sudo drops nvm's PATH — re-run with `sudo -E env PATH=$PATH` "
                 "to enable it)", label, js.name)
    return problems


def inspect(layout: ImageLayout, log: logging.Logger) -> dict[str, Path]:
    """Mount every known partition and return their mount points. Caller is
    responsible for unmounting (the tracker is returned via the dict's
    `_tracker` key).
    """
    log.info("=== Inspecting image (mounts left open) ===")
    tracker = MountTracker()
    mounts: dict[str, Path] = {}
    for label, part in [
        ("boot_a", layout.boot_a),
        ("boot_b", layout.boot_b),
        ("root_a", layout.root_a),
        ("root_b", layout.root_b),
    ]:
        if part is None:
            continue
        ctx = mount_partition(layout.image, part, log, tracker)
        mnt = ctx.__enter__()  # we won't __exit__ — caller cleans up
        mounts[label] = mnt
        log.info("  %s -> %s", label, mnt)
    mounts["_tracker"] = tracker  # type: ignore[assignment]
    return mounts


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def preflight(opts: PatchOptions, log: logging.Logger) -> None:
    """Verify environment + inputs before doing any work."""
    if os.geteuid() != 0:
        raise PreflightError("Must run as root (sudo) — mount and dpkg-deb need root.")

    for cmd in ("sfdisk", "mount", "umount"):
        if not shutil.which(cmd):
            raise PreflightError(f"Required command not found: {cmd}")

    # dpkg-deb is only used to unpack the wireless-regdb .deb, and that patch
    # is skipped outright on images that already ship regulatory.db (Beta 14+),
    # so it isn't a hard requirement unless that .deb is actually going to be
    # used. Filesystem detection reads magic bytes directly, so `file` — which
    # older versions of this check demanded — isn't needed at all.
    if opts.install_regdb and opts.regdb_deb_path.exists():
        if not shutil.which("dpkg-deb"):
            raise PreflightError(
                "Required command not found: dpkg-deb (needed to install "
                f"{opts.regdb_deb_path.name}; pass --no-install-regdb to skip)"
            )

    if not opts.input_image.exists():
        raise PreflightError(f"Input image not found: {opts.input_image}")
    if opts.input_image.stat().st_size < 100 * 1024 * 1024:
        raise PreflightError(
            f"Input image suspiciously small ({opts.input_image.stat().st_size} bytes)"
        )

    if not opts.output_image.parent.exists():
        raise PreflightError(f"Output directory does not exist: {opts.output_image.parent}")

    log.info("Preflight checks passed")


def patch_image(opts: PatchOptions, log: logging.Logger,
                progress: Optional[Callable[[str, int, int], None]] = None) -> Path:
    """Apply all enabled patches. Returns the path to the patched image.

    `progress` is called with (phase_name, current_step, total_steps) so the
    GUI can drive a progress bar.
    """
    preflight(opts, log)

    # If the user picked a .zip output target, force it to .img — we can't
    # patch partitions inside a zip and won't repack at the end.
    if opts.output_image.suffix.lower() == ".zip":
        new_out = opts.output_image.with_suffix(".img")
        log.warning("Output is a .zip; switching to %s (cannot repack a zip)", new_out)
        opts.output_image = new_out

    # Stage 1: copy input -> working file, then -> output at the end. If a
    # patch fails partway through, the user's input image is never touched.
    working = opts.output_image.with_suffix(opts.output_image.suffix + ".partial")

    if opts.backup and opts.input_image == opts.output_image:
        bak = opts.input_image.with_suffix(opts.input_image.suffix + ".bak")
        log.info("Creating backup: %s", bak)
        if not opts.dry_run:
            shutil.copy2(opts.input_image, bak)

    input_is_zip = opts.input_image.suffix.lower() == ".zip"
    if input_is_zip:
        log.info("Extracting %s -> %s", opts.input_image, working)
        if not opts.dry_run:
            if working.exists():
                working.unlink()
            with zipfile.ZipFile(opts.input_image) as zf:
                imgs = [n for n in zf.namelist() if n.lower().endswith(".img")]
                if not imgs:
                    raise RuntimeError(
                        f"No .img file inside {opts.input_image}: {zf.namelist()}"
                    )
                if len(imgs) > 1:
                    log.warning("Multiple .img members found, using first: %s", imgs)
                member = imgs[0]
                log.info("Extracting member %s", member)
                with zf.open(member) as src, open(working, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    else:
        log.info("Copying %s -> %s", opts.input_image, working)
        if not opts.dry_run:
            if working.exists():
                working.unlink()
            shutil.copy2(opts.input_image, working)

    layout = detect_layout(working if not opts.dry_run else opts.input_image, log)

    if opts.force_image_type is not None:
        log.info("Overriding detected type %s -> %s",
                 layout.image_type.value, opts.force_image_type.value)
        layout.image_type = opts.force_image_type

    # Stage 2: patch each partition.
    tracker = MountTracker()
    boot_fn: Callable[[Path, PatchOptions, logging.Logger, str], None] = (
        patch_boot_alpha if layout.image_type == ImageType.ALPHA
        else patch_boot_partition
    )
    try:
        steps: list[tuple[str, Partition, Callable[[Path, PatchOptions, logging.Logger, str], None]]] = []
        if layout.boot_a:
            steps.append(("boot_a", layout.boot_a, boot_fn))
        if layout.boot_b and not opts.skip_b_partitions:
            steps.append(("boot_b", layout.boot_b, boot_fn))
        if layout.root_a:
            steps.append(("rootfs_a", layout.root_a, patch_rootfs_partition))
        if layout.root_b and not opts.skip_b_partitions:
            steps.append(("rootfs_b", layout.root_b, patch_rootfs_partition))

        for i, (label, part, fn) in enumerate(steps, start=1):
            if progress:
                progress(label, i, len(steps))
            if opts.dry_run:
                log.info("[dry-run] Would patch partition %s (offset=%d)",
                         label, part.start_bytes)
                # In dry-run we still walk through to log the sed/copy ops,
                # but we mount the *input* image as read-only just so the
                # patch functions can see the real config.txt.
                # For simplicity in dry-run we don't actually mount anything.
                continue
            with mount_partition(working, part, log, tracker) as mnt:
                fn(mnt, opts, log, label)

    except Exception:
        if opts.cleanup_on_error:
            tracker.cleanup(log)
        else:
            log.error(
                "Patch failed — leaving %d mount(s) open for inspection: %s",
                len(tracker.mounts),
                [str(m) for m in tracker.mounts],
            )
        # Clean up partial output unless the user wanted to keep it for debugging.
        if working.exists() and opts.cleanup_on_error and not opts.keep_mounted:
            log.info("Removing partial output %s", working)
            working.unlink()
        raise

    if opts.keep_mounted:
        log.warning(
            "Patch complete, but mounts left open (--keep-mounted). "
            "Run `umount %s` for each before flashing.",
            " ".join(str(m) for m in tracker.mounts),
        )
    else:
        tracker.cleanup(log)

    if opts.dry_run:
        log.info("[dry-run] Would rename %s -> %s", working, opts.output_image)
        return opts.output_image

    log.info("Renaming %s -> %s", working, opts.output_image)
    if opts.output_image.exists():
        opts.output_image.unlink()
    working.rename(opts.output_image)

    if opts.validate_after:
        layout = detect_layout(opts.output_image, log)
        validate(layout, log)

    log.info("Done. Patched image: %s", opts.output_image)
    return opts.output_image

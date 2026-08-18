"""Windows-native image patching core.

Same patch logic as patcher/core.py but uses:
- Pure Python MBR parsing (no sfdisk)
- pyfatfs for FAT32 boot partitions (no mount)
- debugfs.exe for ext4 rootfs partitions (no mount)
- Pure Python .deb extraction (no dpkg-deb)
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .partition import ImageLayout, Partition, detect_layout
from .fat import FatPartition
from .ext4 import Ext4Partition, _find_debugfs
from .deb import extract_deb_data


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent

RESOURCES = PROJECT_ROOT / "patcher" / "resources"
RES_UDEV_CAN = RESOURCES / "90-usb-can-rename.rules"
RES_CANBUSPROCESS = RESOURCES / "canbusprocess-override.conf"
RES_CANBUSWATCHDOG = RESOURCES / "canbuswatchdog-override.conf"
RES_ROBOT = RESOURCES / "robot-override.conf"
RES_PICOFLASHER = RESOURCES / "picoflasher-override.conf"
RES_MRCCAN = RESOURCES / "mrccan.conf"
RES_MODULES_LOAD = RESOURCES / "modules-load.conf"

DEFAULT_FLASH_PICO = PROJECT_ROOT / "netboot" / "flash-pico.sh"
DEFAULT_REGDB_DEB = (
    PROJECT_ROOT / "beta9" / "wireless-regdb_2025.10.07-0ubuntu1~24.04.1_all.deb"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PatcherError(Exception):
    pass


class PreflightError(PatcherError):
    pass


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------


@dataclass
class PatchOptions:
    input_image: Path = Path("")
    output_image: Path = Path("")
    flash_pico_path: Path = DEFAULT_FLASH_PICO
    regdb_deb_path: Path = DEFAULT_REGDB_DEB

    # Boot patches
    enable_hdmi: bool = True
    disable_spi_can: bool = True
    update_cmdline: bool = True

    # Rootfs patches
    install_flash_pico: bool = True
    install_can_udev: bool = True
    install_canbusprocess: bool = True
    install_canbuswatchdog: bool = True
    install_robot_override: bool = True
    install_mrccan: bool = True
    install_modules_load: bool = True
    install_regdb: bool = True
    patch_dashboard_wlan: bool = True
    patch_dashboard_faults: bool = True

    # Operational
    dry_run: bool = False
    verbose: bool = False
    backup: bool = False
    skip_b_partitions: bool = False
    validate_after: bool = False

    def patch_names(self) -> list[str]:
        return [
            "enable_hdmi", "disable_spi_can", "update_cmdline",
            "install_flash_pico", "install_can_udev",
            "install_canbusprocess", "install_canbuswatchdog",
            "install_robot_override", "install_mrccan", "install_modules_load",
            "install_regdb",
            "patch_dashboard_wlan", "patch_dashboard_faults",
        ]


PATCH_DESCRIPTIONS: dict[str, str] = {
    "enable_hdmi": "Uncomment HDMI display options in config.txt",
    "disable_spi_can": "Comment out spi/sc-mcp2518 overlays (no SPI CAN on Pi 5B)",
    "update_cmdline": "Add panic=0 and cfg80211.ieee80211_regdom=US to cmdline.txt",
    "install_flash_pico": "Install flash-pico.sh + picoflasherprocess override",
    "install_can_udev": "Install 90-usb-can-rename.rules (USB-only trigger)",
    "install_canbusprocess": "Install canbusprocess override with vcan placeholders",
    "install_canbuswatchdog": "Install canbuswatchdog override (waits for any can_s*)",
    "install_robot_override": "Install robot.service override (30s CAN wait, optional)",
    "install_mrccan": "Install /etc/tmpfiles.d/mrccan.conf (unblocks MrcCommDaemon)",
    "install_modules_load": "Load robot_heartbeat + i2c-dev at boot (creates /dev/mrccan/*)",
    "install_regdb": "Install wireless-regdb so WiFi works on US regulatory domain",
    "patch_dashboard_wlan": "Unlock WLAN0 AP settings in the dashboard JS",
    "patch_dashboard_faults": "Add a 'Reset Fault Counts' button to the dashboard",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sed(text: str, pattern: str, replacement: str, log: logging.Logger) -> tuple[str, int]:
    """Apply regex substitution, return (new_text, count)."""
    patched, count = re.subn(pattern, replacement, text, flags=re.MULTILINE)
    if count > 0:
        log.info("  sed: %d substitution(s) for %r", count, pattern[:60])
    return patched, count


# ---------------------------------------------------------------------------
# Boot partition patches (FAT32 via pyfatfs)
# ---------------------------------------------------------------------------


def patch_boot_partition(fat: FatPartition, opts: PatchOptions,
                         log: logging.Logger, label: str) -> None:
    log.info("=== Patching boot partition %s ===", label)

    if opts.enable_hdmi:
        log.info("[%s] Enabling HDMI", label)
        config = fat.read_text("/config.txt")
        for pat, repl in [
            (r"^hdmi_ignore_hotplug=1", "#hdmi_ignore_hotplug=1"),
            (r"^hdmi_ignore_edid=0xa5000080", "#hdmi_ignore_edid=0xa5000080"),
            (r"^hdmi_blanking=2", "#hdmi_blanking=2"),
            (r"^ignore_lcd=1", "#ignore_lcd=1"),
            (r"^display_auto_detect=0", "display_auto_detect=1"),
        ]:
            config, _ = _sed(config, pat, repl, log)
        fat.write_text("/config.txt", config)

    if opts.disable_spi_can:
        log.info("[%s] Commenting out SPI CAN overlays", label)
        config = fat.read_text("/config.txt")
        config, _ = _sed(config, r"^(dtoverlay=spi[0-9].*)$", r"#\1", log)
        config, _ = _sed(config, r"^(dtoverlay=sc-mcp2518.*)$", r"#\1", log)
        fat.write_text("/config.txt", config)

    if opts.update_cmdline:
        try:
            content = fat.read_text("/cmdline.txt").rstrip("\n")
        except Exception:
            log.warning("[%s] cmdline.txt not found", label)
            return
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
                fat.write_text("/cmdline.txt", content + "\n")


# ---------------------------------------------------------------------------
# Rootfs patches (ext4 via debugfs)
# ---------------------------------------------------------------------------


def patch_rootfs_partition(ext4: Ext4Partition, opts: PatchOptions,
                           log: logging.Logger, label: str) -> None:
    log.info("=== Patching rootfs %s ===", label)

    if opts.install_flash_pico:
        if not opts.flash_pico_path.exists():
            log.warning("[%s] flash-pico.sh not found at %s, skipping",
                        label, opts.flash_pico_path)
        else:
            log.info("[%s] Installing flash-pico.sh", label)
            if not opts.dry_run:
                ext4.copy_file_in(opts.flash_pico_path,
                                  "/usr/local/bin/flash-pico.sh", mode=0o755)
                ext4.copy_file_in(
                    RES_PICOFLASHER,
                    "/etc/systemd/system/limelight_picoflasherprocess.service.d/override.conf",
                )

    if opts.install_can_udev:
        log.info("[%s] Installing CAN udev rules", label)
        if not opts.dry_run:
            ext4.copy_file_in(RES_UDEV_CAN, "/etc/udev/rules.d/90-usb-can-rename.rules")

    if opts.install_canbusprocess:
        log.info("[%s] Installing canbusprocess override", label)
        if not opts.dry_run:
            ext4.copy_file_in(
                RES_CANBUSPROCESS,
                "/etc/systemd/system/limelight_canbusprocess.service.d/override.conf",
            )

    if opts.install_canbuswatchdog:
        log.info("[%s] Installing canbuswatchdog override", label)
        if not opts.dry_run:
            ext4.copy_file_in(
                RES_CANBUSWATCHDOG,
                "/etc/systemd/system/limelight_canbuswatchdog.service.d/override.conf",
            )

    if opts.install_robot_override:
        log.info("[%s] Installing robot.service override", label)
        if not opts.dry_run:
            ext4.copy_file_in(
                RES_ROBOT,
                "/etc/systemd/system/robot.service.d/override.conf",
            )

    if opts.install_mrccan:
        log.info("[%s] Installing mrccan tmpfiles.d config", label)
        if not opts.dry_run:
            ext4.copy_file_in(RES_MRCCAN, "/etc/tmpfiles.d/mrccan.conf")

    if opts.install_modules_load:
        log.info("[%s] Installing modules-load.d config", label)
        if not opts.dry_run:
            ext4.copy_file_in(RES_MODULES_LOAD,
                              "/etc/modules-load.d/systemcore-pi5b.conf")

    if opts.install_regdb:
        if ext4.exists("/usr/lib/firmware/regulatory.db"):
            log.info("[%s] regulatory.db already present upstream, skipping regdb install",
                     label)
        elif not opts.regdb_deb_path.exists():
            log.warning("[%s] wireless-regdb .deb not found at %s, skipping",
                        label, opts.regdb_deb_path)
        else:
            log.info("[%s] Installing wireless-regdb", label)
            if not opts.dry_run:
                _install_regdb(ext4, opts.regdb_deb_path, log, label)

    if opts.patch_dashboard_wlan or opts.patch_dashboard_faults:
        _patch_dashboard(ext4, opts, log, label)


def _install_regdb(ext4: Ext4Partition, deb: Path, log: logging.Logger, label: str) -> None:
    """Extract regdb .deb and install firmware files into ext4 rootfs."""
    with tempfile.TemporaryDirectory(prefix="regdb-") as tmpdir:
        extract_deb_data(deb, Path(tmpdir))
        fw_src = Path(tmpdir) / "lib" / "firmware"
        if not fw_src.exists():
            fw_src = Path(tmpdir) / "usr" / "lib" / "firmware"
        if fw_src.exists():
            for f in fw_src.iterdir():
                if f.is_file():
                    ext4.copy_file_in(f, f"/usr/lib/firmware/{f.name}")
        else:
            log.warning("[%s] No firmware directory found in .deb", label)


def _patch_dashboard(ext4: Ext4Partition, opts: PatchOptions,
                     log: logging.Logger, label: str) -> None:
    """Read dashboard JS, apply regex patches, write back."""
    js_dir = "/var/www/html/static/js"

    # List the js directory to find main.*.js
    # Use debugfs ls to find the file
    out = ext4._run_debugfs([f"ls {js_dir}"])
    matches = re.findall(r"main\.[a-f0-9]+\.js", out)
    if not matches:
        log.warning("[%s] Dashboard main.*.js not found, skipping", label)
        return

    js_name = matches[0]
    js_path = f"{js_dir}/{js_name}"
    log.info("[%s] Patching dashboard: %s", label, js_name)

    if opts.dry_run:
        return

    content = ext4.read_text(js_path)

    if opts.patch_dashboard_wlan:
        content, _ = _sed(content, r"disabled:o\|\|a", "disabled:o", log)
        content, _ = _sed(
            content,
            r',\{static_ip:"172\.30\.0\.1",gateway:"172\.30\.0\.1",use_dhcp:!1\}',
            ",{}",
            log,
        )

    if opts.patch_dashboard_faults:
        content, _ = _sed(
            content,
            r"faultCounts:t\.fc\|\|\[0,0,0,0,0,0\]",
            "faultCounts:(window.__rawFC=t.fc||[0,0,0,0,0,0]).map(function(v,j){"
            "return Math.max(0,v-((window.__faultBL||[])[j]||0))})",
            log,
        )
        content = _inject_fault_reset_button(content, log, label)

    ext4.write_text(js_path, content)


# Marker used to make the fault-button injection idempotent.
FAULT_RESET_MARKER = "window.__faultBL=window.__rawFC"

# End of the historical fault list — the `]` closes the tooltip's children
# array, which is where the button gets appended.
FAULT_LIST_TAIL_RE = r'"historical-"\.concat\(t\)\)\}\)\)\]'

# The bundler minifies the react/jsx-runtime import to a different name in
# every build ("xo" in Beta 10, "bo" in Beta 14), so the alias has to be read
# out of the surrounding code rather than hardcoded — the injected markup
# throws a ReferenceError and blanks the dashboard if it names the wrong one.
JSX_ALIAS_RE = r"\(0,(\w+)\.jsx\)"


def _inject_fault_reset_button(content: str, log: logging.Logger,
                               label: str) -> str:
    """Append a "Reset Fault Counts" button to the dashboard fault tooltip."""
    if FAULT_RESET_MARKER in content:
        log.info("[%s] Fault reset button already present, skipping", label)
        return content

    m = re.search(FAULT_LIST_TAIL_RE, content)
    if not m:
        log.warning("[%s] Fault history list not found — dashboard layout changed "
                    "upstream; skipping fault reset button", label)
        return content

    # The nearest jsx() call before the list is the one rendering it.
    aliases = re.findall(JSX_ALIAS_RE, content[max(0, m.start() - 500):m.start()])
    if not aliases:
        log.warning("[%s] Could not determine the jsx alias near the fault list; "
                    "skipping fault reset button", label)
        return content

    jsx = aliases[-1]
    button = (
        f'"historical-".concat(t))}})),'
        f'(0,{jsx}.jsx)("div",{{style:{{marginTop:"8px",textAlign:"center"}},'
        f'children:(0,{jsx}.jsx)("button",{{onClick:function(){{'
        f'{FAULT_RESET_MARKER}?window.__rawFC.slice():[]}},'
        f'style:{{fontSize:"11px",padding:"2px 8px",cursor:"pointer",'
        f'background:"#333",color:"#fff",border:"1px solid #666",'
        f'borderRadius:"3px"}},children:"Reset Fault Counts"}})}})]'
    )
    log.info("[%s] Injecting fault reset button (jsx alias: %s)", label, jsx)
    return content[:m.start()] + button + content[m.end():]


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate(layout: ImageLayout, log: logging.Logger) -> list[str]:
    """Verify expected post-patch files exist in rootfs partitions."""
    log.info("=== Validating patched image ===")
    problems: list[str] = []

    for label, part in [("rootfs_a", layout.root_a), ("rootfs_b", layout.root_b)]:
        if part is None:
            continue
        with Ext4Partition(layout.image, part.start_bytes, part.size_bytes) as ext4:
            expected = [
                "/etc/tmpfiles.d/mrccan.conf",
                "/etc/modules-load.d/systemcore-pi5b.conf",
                "/etc/udev/rules.d/90-usb-can-rename.rules",
                "/etc/systemd/system/limelight_canbusprocess.service.d/override.conf",
                "/etc/systemd/system/robot.service.d/override.conf",
                "/usr/local/bin/flash-pico.sh",
            ]
            for rel in expected:
                if not ext4.exists(rel):
                    problems.append(f"[{label}] missing: {rel}")
                else:
                    log.debug("[%s] ok: %s", label, rel)

    if not problems:
        log.info("Validation passed")
    else:
        for p in problems:
            log.error(p)
    return problems


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def preflight(opts: PatchOptions, log: logging.Logger) -> None:
    """Verify inputs before doing any work."""
    # No root check — Windows doesn't need it for file-level access
    try:
        _find_debugfs()
    except FileNotFoundError as e:
        raise PreflightError(str(e))

    try:
        import pyfatfs  # noqa: F401
    except ImportError:
        raise PreflightError(
            "pyfatfs not installed. Run: pip install pyfatfs"
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
    """Apply all enabled patches. Returns path to the patched image."""
    preflight(opts, log)

    if opts.output_image.suffix.lower() == ".zip":
        opts.output_image = opts.output_image.with_suffix(".img")
        log.warning("Output is .zip; switching to %s", opts.output_image)

    working = opts.output_image.with_suffix(opts.output_image.suffix + ".partial")

    if opts.backup and opts.input_image == opts.output_image:
        bak = opts.input_image.with_suffix(opts.input_image.suffix + ".bak")
        log.info("Creating backup: %s", bak)
        if not opts.dry_run:
            shutil.copy2(opts.input_image, bak)

    # Extract or copy input to working file
    if opts.input_image.suffix.lower() == ".zip":
        log.info("Extracting %s", opts.input_image)
        if not opts.dry_run:
            if working.exists():
                working.unlink()
            with zipfile.ZipFile(opts.input_image) as zf:
                imgs = [n for n in zf.namelist() if n.lower().endswith(".img")]
                if not imgs:
                    raise PatcherError(f"No .img file inside {opts.input_image}")
                member = imgs[0]
                log.info("Extracting member %s", member)
                with zf.open(member) as src, open(working, "wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
    else:
        log.info("Copying %s -> %s", opts.input_image.name, working.name)
        if not opts.dry_run:
            if working.exists():
                working.unlink()
            shutil.copy2(opts.input_image, working)

    target = working if not opts.dry_run else opts.input_image
    layout = detect_layout(target)

    log.info("Partition layout:")
    for label, part in [("boot_a", layout.boot_a), ("boot_b", layout.boot_b),
                        ("root_a", layout.root_a), ("root_b", layout.root_b)]:
        if part:
            log.info("  %s: partition %d, %s, offset=%d, size=%.1f MB",
                     label, part.index, part.fs, part.start_bytes,
                     part.size_bytes / (1024 * 1024))
    image_size = target.stat().st_size  # `working` doesn't exist in dry-run
    for part in layout.partitions:
        if part.present:
            continue
        if part.start_bytes < image_size:
            # The file ends inside this partition: truncated download, not a
            # packed image (a packed image's missing partitions start past EOF).
            log.warning("  partition %d (offset %d, %.1f MB) is cut off by the end "
                        "of the file — the image looks truncated or incompletely "
                        "downloaded. Skipping it; re-download if that's unexpected.",
                        part.index, part.start_bytes, part.size_bytes / (1024 * 1024))
        else:
            log.info("  partition %d (offset %d, %.1f MB) lies past end of file — "
                     "not present in this image",
                     part.index, part.start_bytes, part.size_bytes / (1024 * 1024))
    if layout.packed:
        log.info("Packed image: rootfs B is not in the file (created on first boot "
                 "by expandfs.sh). Patching rootfs A only.")

    # Patch boot partitions (FAT32)
    boot_parts = [("boot_a", layout.boot_a)]
    if not opts.skip_b_partitions and layout.boot_b:
        boot_parts.append(("boot_b", layout.boot_b))

    root_parts = [("rootfs_a", layout.root_a)]
    if not opts.skip_b_partitions and layout.root_b:
        root_parts.append(("rootfs_b", layout.root_b))

    total_steps = len(boot_parts) + len(root_parts)
    step = 0

    for label, part in boot_parts:
        if part is None:
            continue
        step += 1
        if progress:
            progress(label, step, total_steps)
        if opts.dry_run:
            log.info("[dry-run] Would patch %s", label)
            continue
        with FatPartition(working, part.start_bytes, part.size_bytes) as fat:
            patch_boot_partition(fat, opts, log, label)

    for label, part in root_parts:
        if part is None:
            continue
        step += 1
        if progress:
            progress(label, step, total_steps)
        if opts.dry_run:
            log.info("[dry-run] Would patch %s", label)
            continue
        with Ext4Partition(working, part.start_bytes, part.size_bytes) as ext4:
            patch_rootfs_partition(ext4, opts, log, label)

    # Finalize
    if opts.dry_run:
        log.info("[dry-run] Would rename %s -> %s", working, opts.output_image)
        return opts.output_image

    log.info("Renaming %s -> %s", working.name, opts.output_image.name)
    if opts.output_image.exists():
        opts.output_image.unlink()
    working.rename(opts.output_image)

    if opts.validate_after:
        layout = detect_layout(opts.output_image)
        validate(layout, log)

    log.info("Done. Patched image: %s", opts.output_image)
    return opts.output_image

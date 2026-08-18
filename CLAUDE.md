# SystemCore RPi5 Guide

## What this project is

Tooling for running FRC Limelight SystemCore OS on a standard Raspberry Pi 5 Model B (instead of the Compute Module 5). A single `build-image.sh` script produces a ready-to-flash SD card image with zero post-flash interaction.

## Supported upstream images

| Family | Releases | Partition layout | Kernel | Detection |
| --- | --- | --- | --- | --- |
| `alpha` | Alpha 2–10 (`limelightsystemcorecm5.zip`) | 4 partitions: 1 boot FAT32, 2 rootfs ext4, 1 data | Linux 6.6.45, 4K pages | Single FAT32 boot partition |
| `ab` | Beta 10+ **and** Alpha 11+ | 6 partitions: boot selector + 2 boot FAT32 + extended + 2 rootfs ext4 | Linux 6.12.77, 16K pages | Two FAT32 boot partitions |

Since Alpha 11 the alpha and beta lines ship the same partition table, so both patch
identically — `alpha` in the table above means only the *legacy* Alpha 2–10 layout.
The patcher auto-detects from the partition layout; `--image-type alpha|ab` overrides
(`beta`/`beta10` are accepted spellings of `ab`).

Current target: **`limelightosr-2027.0.0-beta14-201`** (Aug 2026). `build-image.sh` pins the
release in `RELEASE_TAG`/`RELEASE_NAME`; new releases are listed at
https://github.com/LimelightVision/systemcore-os-public/releases

### Packed images (Beta 14 / Alpha 14 and later)

Upstream now ships the image packed: rootfs A is shrunk to its filesystem size (2 GiB) and
rootfs B is declared in the partition table but lies past end-of-file — the whole `.img` is
~2.2 GB instead of ~15 GB. On first boot `/usr/local/bin/expandfs.sh`
(`limelight_expandfs.service`) rewrites the table, grows both slots to 7 GiB, resizes rootfs
A online, and mkfs's an empty slot B.

Consequences for the patcher:
- Partitions that don't fit inside the file are marked `present=False` and never mounted
  (`Partition.present`, `ImageLayout.packed`).
- Only rootfs A gets patched. Slot B ships empty and is only ever filled by an OTA update,
  which `dd`s a whole rootfs over the slot — so patches there would be wiped anyway.
- After an OTA update to slot B, the patches are gone from the running system. Re-flash a
  patched image (or re-apply the patches to the new slot) rather than relying on OTA.

## Primary workflow

```bash
# Build from scratch (downloads the pinned upstream release, then patches it)
sudo ./build-image.sh
sudo dd if=systemcore-pi5b-2027.0.0-beta14-v1.img of=/dev/sdX bs=4M status=progress

# Any upstream image (Alpha or Beta, auto-detected)
sudo python3 patch-image.py upstream.img
sudo dd if=upstream-pi5b.img of=/dev/sdX bs=4M status=progress
```

## Project layout

```
build-image.sh        - Downloads the pinned upstream release, then calls patch-image.py
patch-image.py        - Standalone patcher for new upstream releases
patcher/              - Python package backing patch-image.py
  core.py             - Partition discovery, mount tracking, per-patch logic, orchestrator
  gui.py              - Tkinter GUI with live log streaming + per-patch toggles
  cli.py              - argparse, --dry-run / --inspect / --validate / --only
  resources/          - Drop-in service overrides + udev rules + tmpfile/modules-load configs
    alpha2/           - Pi 5B DTBs compiled from Alpha 2 kernel (6.6.45)
netboot/
  flash-pico.sh       - Pico flasher replacement (installed into image by build-image.sh)
  setup-netboot.sh    - TFTP + NFS netboot setup for WSL2 development
  cmdline_nfs.txt     - NFS boot cmdline template
```

## When to use which tool

| Scenario | Tool |
| --- | --- |
| Build a patched image from scratch | `sudo ./build-image.sh` |
| New upstream release | `sudo python3 patch-image.py upstream.img` |
| Apply only one patch to an existing image for debugging | `sudo python3 patch-image.py img --only install_mrccan` |
| Inspect what's in a patched image | `sudo python3 patch-image.py img --inspect` (mounts every partition present, prints paths) |
| Verify a patched image looks right | `sudo python3 patch-image.py img --validate` |
| See what would happen without actually patching | `sudo python3 patch-image.py img --dry-run -v` |

`patcher/resources/*` is the single source of truth for systemd overrides, udev rules, and tmpfile configs. `build-image.sh` delegates all patching to `patch-image.py`, so there is only one copy of each patch — edit the resource file and both paths pick it up.

Not tracked in git: `cache/`, `*.img`, `*.zip`, `netboot/tftpboot/`, `netboot/nfsroot/`

## What the build script patches

1. **HDMI enabled** — stock image disables all display output (headless for Limelight carrier board)
2. **SPI CAN overlays disabled** — no MCP2518 hardware on Pi 5B
3. **flash-pico.sh** — replaces picoflasherprocess for external USB Pico flashing via dd
4. **Multi-adapter USB-CAN** — any number of USB-CAN adapters, CAN FD with fallback to standard CAN
5. **Persistent CAN naming** — USB port path mapped to stable can_sN index via `/etc/can_port_map`, works with USB hubs
6. **CAN is optional** — 30s timeout, robot starts regardless of adapter presence
7. **CAN discovery frame** — `cansend 000#00` sent on each bus after interface up
8. **vcan placeholders** — canbusprocess fills missing `can_s0..can_s4` slots with vcan interfaces after USB-CAN setup. HAL aborts the robot program if any of the 5 buses is missing (`SIOCGIFINDEX ... No such device`), so this is required for robot.service to start with fewer than 5 physical adapters.
9. **/dev/mrccan tmpfile** — `/etc/tmpfiles.d/mrccan.conf` creates `/dev/mrccan/` at boot. Without it `MrcCommDaemon` crash-loops on `Failed to open control data file`, never sets the NT key `/Netcomm/Control/ServerReady`, and the HAL SIGABRTs the robot program ~10s after start with `Error: Waiting for server ready failed`.
10. **robot_heartbeat + i2c-dev loaded at boot** — `/etc/modules-load.d/systemcore-pi5b.conf`. Upstream modprobes these at the tail of `limelight_canbusprocess.service`'s ExecStart, which this project replaces wholesale — so without this file they never load. `robot_heartbeat` (pulls in `can_sender`) registers the misc devices `mrccan!controldata`, `mrccan!matchinfo`, `mrccan!onbot_override` and their read-only variants, i.e. the real `/dev/mrccan/*` nodes MrcCommDaemon opens. Loading it via modules-load.d rather than from the service means the nodes exist before `mrccomm.service` starts, so MrcCommDaemon can't get there first and leave regular files on those paths. The tmpfile above stays as the fallback for when the module is missing or fails to load.
11. **Wireless regdb** — regulatory.db installed for US WiFi channel support. Skipped when the image already ships `/usr/lib/firmware/regulatory.db` (true since Beta 14).
12. **WLAN0 AP settings unlocked** — dashboard JS patched to allow modifying Access Point config
13. **Fault count reset button** — frontend-only baseline reset added to fault tooltip in dashboard

## Legacy-Alpha patches (auto-applied only for the Alpha 2–10 layout)

Alpha 2 through Alpha 10 have a different boot layout and need these additional fixes on top
of the shared patches above. Alpha 11+ ships the A/B layout and needs none of them (it already
carries `bcm2712-rpi-5-b.dtb`, an `[all]` config.txt section, and a real `cmdline.txt`):

1. **autoboot.txt disabled** — Alpha's autoboot.txt has `boot_partition=2` which redirects the Pi 5 bootloader to the ext4 rootfs partition (unreadable). Renamed to `.disabled`.
2. **config.txt [all] section** — Alpha's config.txt has `[boot_partition=2]` / `[boot_partition=3]` conditional sections. `kernel=Image` and all overlays fall inside the `[boot_partition=3]` section and are invisible when booting from partition 1. `[all]` is inserted before the global settings.
3. **cmdline.txt created** — config.txt references `cmdline.txt` but only `cmdline_a.txt` and `cmdline_b.txt` exist. `cmdline.txt` is created as a copy of `cmdline_a.txt` (rootfs A).
4. **Pi 5B device tree installed** — Alpha images only ship `bcm2712-rpi-cm5-cm5io.dtb` (Limelight carrier board). The Pi 5B bootloader needs `bcm2712-rpi-5-b.dtb` which is installed from `patcher/resources/alpha2/` (compiled from the Alpha 2 kernel 6.6.45).
5. **Pi 4 firmware commented out** — `start_file=start4.elf` and `fixup_file=fixup4.dat` are Pi 4 directives, commented out for Pi 5.

## Key technical details

- Current upstream image: `limelightsystemcorebetacm5-limelightosr-2027.0.0-beta14.zip`
  (release `limelightosr-2027.0.0-beta14-201`); the matching alpha is
  `limelightsystemcorecm5-limelightosr-2027.0.0-alpha14.zip` and patches identically
- Alpha 2 upstream image: `limelightsystemcorecm5.zip` (release `Limelight_SYSTEMCORE-159`)
- A/B partition layout (6 partitions), Beta 10+ / Alpha 11+:
  - p1: boot selector (FAT32, 16M) — autoboot.txt (`boot_partition=2`, `tryboot`→3)
  - p2: boot A (FAT32, 64M) — kernel (Image), DTBs incl. `bcm2712-rpi-5-b.dtb`, config.txt, cmdline.txt (`root=/dev/mmcblk0p5`)
  - p3: boot B (FAT32, 64M) — same as boot A but `root=/dev/mmcblk0p6`
  - p4: extended
  - p5: rootfs A (ext4, 7G — ships as 2G in packed images)
  - p6: rootfs B (ext4, 7G — absent from packed images, created on first boot)
- Alpha partition layout (4 partitions):
  - p1: boot (FAT32, 64M) — single partition with kernel (Image), DTBs, config.txt, cmdline_a/b.txt, autoboot.txt, overlays
  - p2: rootfs A (ext4, ~5G)
  - p3: rootfs B (ext4, ~5G)
  - p4: data (~256M)
- Beta 10+ kernel: Linux 6.12.77, 16K pages (stock kernel works on Pi 5B; still 6.12.77-v8-16k in Beta 14)
- Alpha 2 kernel: Linux 6.6.45, 4K pages (needs Pi 5B DTB from patcher/resources/alpha2/)
- Pi 5B external USB: xhci-hcd.0 (bus 1, ports 1-1 through 1-2)
- Pico after flash: VID=0xCAFE PID=0x4011 ("Limelight RT Subsystem")
- CAN udev match: `ATTR{type}=="280"` (ARPHRD_CAN, matches any CAN netdev). The build's `90-usb-can-rename.rules` adds `SUBSYSTEMS=="usb"` so vcan interfaces (no parent device) don't match — without that constraint, adding a vcan from inside canbusprocess re-triggers canbusprocess and infinite-loops.
- CAN FD: 1Mbps nominal / 5Mbps data bitrate, falls back to 1Mbps standard CAN if adapter doesn't support FD
- CAN port mapping persisted to `/etc/can_port_map` (port path -> can_sN index)
- HAL CAN expectation: WPILib's HAL (`libwpiHal.so`, `_GLOBAL__N_1::SocketCanState::InitializeBuses`) does `SIOCGIFINDEX` on `can_s0` through `can_s4`. ANY missing one → `IllegalStateException: Failed to initialize. Terminating` from `org.wpilib.framework.RobotBase.startRobot`. canbusprocess fills gaps with vcan after USB-CAN setup.
- HAL netcomm gate: HAL also blocks on NT key `/Netcomm/Control/ServerReady` (string lives in `libwpiHal.so`). The setter is `/usr/bin/MrcCommDaemon` (`mrccomm.service`), which opens `/dev/mrccan/controldata` + `/dev/mrccan/matchinfo` (`O_WRONLY|O_CREAT|O_TRUNC`). Those paths are the misc devices registered by the `robot_heartbeat` module (`kernel/net/can/robot_heartbeat.ko.xz`, depends on `can_sender`) — not carrier-board-specific hardware, it just has to be loaded, which the build now does via `/etc/modules-load.d/systemcore-pi5b.conf`. `/etc/tmpfiles.d/mrccan.conf` remains as the fallback: with the bare directory present, MrcCommDaemon creates plain files and still publishes ServerReady. Without either: `Failed to open control data file` → mrccomm crash-loops → HAL `Waiting for server ready failed` → SIGABRT (exit 134) ~10s after Java starts.
- RP2350 firmware faults (BROWNOUT, IMU, DISPLAY, CAN, RSL) are cosmetic — closed-source firmware expects carrier board hardware
- Dashboard patches: sed on minified React JS (`main.*.js`), applied to every rootfs slot present. The fault-reset button injection must read the minified react/jsx-runtime alias out of the surrounding code (`xo` in Beta 10, `bo` in Beta 14) — hardcoding it throws a ReferenceError and blanks the dashboard. `node --check` on the patched JS catches this.
- Interface renaming done in canbusprocess service (NOT udev PROGRAM — `ip link show` is unreliable in udev context)
- Systemd ExecStart must not use `${VAR##pattern}` syntax — systemd strips `${...}` before bash sees it
- `patcher_win` (the no-WSL path) writes partitions directly, and three of its primitives were silently wrong until they were exercised end-to-end against Beta 14. Re-check these if that code is touched: FAT lookups must be case-insensitive (pyfatfs reports 8.3 entries upper-cased, so `/config.txt` misses `CONFIG.TXT`); pyfatfs closes the stream handed to it, so the buffer must survive `close()` for the write-back; and `debugfs` needs `set_inode_field <path> mode 0100644` (with the S_IFREG bits) plus a guard against `mkdir` on paths that already exist, or the result is a filesystem that fails `e2fsck` and throws EIO on readdir. Validate any change with `e2fsck -fn` on the extracted rootfs — the patcher's own `--validate` passed on an image the kernel refused to read.
- Upstream ships `limelight_canbusprocess.service` as `Type=oneshot`/`RemainAfterExit=yes` (Beta 14). Our drop-in's ExecStart never returns and sets `Restart=always`, which systemd refuses on a oneshot unit ("isn't allowed for Type=oneshot services. Refusing.") — so the drop-in also sets `Type=simple`/`RemainAfterExit=no`. Check drop-ins against a new release with `systemd-analyze verify <unit>` before flashing.
- Upstream `robot.service` (Beta 14) waits up to 15s for `can_s0..can_s4` to be `state UP` and starts anyway on timeout; our override replaces that ExecStartPre with a 30s wait
- Upstream `70-can-interface-names.rules` names the carrier board's SPI CAN controllers (`spi2.0` → `can_s0`, etc.). It never matches on Pi 5B since the SPI overlays are commented out, so it doesn't conflict with `90-usb-can-rename.rules`.

## Diagnosing a non-starting robot.service on an already-flashed image

```bash
sudo journalctl -u robot.service -n 50 --no-pager
```

| Symptom (journal line) | Cause | Fix |
| --- | --- | --- |
| `ioctl(SIOCGIFINDEX) for CAN can_sN failed with No such device` then `Failed to initialize. Terminating` | Slot `can_sN` is missing from `/sys/class/net/` (fewer than 5 USB CAN adapters and the vcan-placeholder logic isn't running) | `sudo modprobe vcan && sudo ip link add dev can_sN type vcan && sudo ip link set can_sN up` for each missing N. If a service is deleting them, check `/etc/udev/rules.d/90-usb-can-rename.rules` — must include `SUBSYSTEMS=="usb"`, else `ip link add` of a vcan re-triggers canbusprocess which has a "delete CAN interfaces without `device/driver`" cleanup that wipes the vcan you just made. |
| `Error: Waiting for server ready failed. Restarting app and retrying...` then `terminate called without an active exception` and `Aborted (core dumped)` (exit 134) | `MrcCommDaemon` isn't setting `/Netcomm/Control/ServerReady` in NT4 — almost always because it's crash-looping on `Failed to open control data file` | First try `sudo modprobe robot_heartbeat && sudo systemctl restart mrccomm.service` — that registers the real `/dev/mrccan/*` devices. If the module is unavailable, `sudo mkdir -p /dev/mrccan && sudo systemctl restart mrccomm.service`. For persistence: `/etc/modules-load.d/systemcore-pi5b.conf` listing `robot_heartbeat`, plus `/etc/tmpfiles.d/mrccan.conf` with `d /dev/mrccan 0755 root root -`. Note `ls -l /dev/mrccan` — if the entries are regular files rather than character devices, the module lost the race and you should delete them before reloading it. |
| `Failed to initialize can buses` for `can_d2` (not `can_s2`) | Both `can_s*` AND `can_d*` are probed by the HAL. `can_d0..can_d19` are typically created by stock SystemCore-OS init; if they're missing the image is broken — don't rename `can_d*` interfaces away. | Reboot to let stock init recreate them, or `sudo modprobe vcan && for i in $(seq 0 19); do sudo ip link add dev can_d$i type vcan; sudo ip link set can_d$i up; done` |

## Dev environment

- Host: WSL2 on Windows (Ubuntu/Debian)
- Target: Raspberry Pi 5 Model B (BCM2712), ARM64
- SystemCore version: 2027.0.0 Beta 14 (limelightosr-2027.0.0-beta14-201)
- Test Pi: systemcore@10.0.0.167 (password: systemcore)

## Network boot (for development)

WSL2 serves TFTP + NFS to Pi 5 over the LAN. Requires mirrored networking mode in `.wslconfig`. Run `netboot/setup-netboot.sh` to configure, then set Pi EEPROM boot order to `0xf21`.

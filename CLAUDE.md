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

Current target: **`limelightosr-2027.0.0-beta14-210`** (Sep 2026), which replaced the
deleted `beta14-201`. Robot programs on it need **WPILib 2027 Alpha 7+** (upstream release
note; the HAL ships with the robot code, so there is nothing to patch). `build-image.sh`
pins the release in `RELEASE_TAG`/`RELEASE_NAME` and caches the zip under
`cache/<RELEASE_TAG>/` — upstream reuses zip filenames across rebuilds of a release, so a
name-keyed cache silently serves the stale image after a tag bump. New releases are listed
at https://github.com/LimelightVision/systemcore-os-public/releases

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
sudo dd if=systemcore-pi5b-2027.0.0-beta14-v7.img of=/dev/sdX bs=4M status=progress

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
  dashboard.py        - Minified-dashboard JS transforms, shared with patcher_win
  gui.py              - Tkinter GUI with live log streaming + per-patch toggles
  cli.py              - argparse, --dry-run / --inspect / --validate / --only
  resources/          - Drop-in service overrides + udev rules + tmpfile/modules-load configs
    camera-shim/      - realpath() LD_PRELOAD shim (+ source, build.sh) that lets vision servers see a camera on a bare Pi 5B port
    alpha2/           - Pi 5B DTBs compiled from Alpha 2 kernel (6.6.45)
examples/
  phoenix-tuner-robot/ - Minimal WPILib+Phoenix program that brings up the Phoenix diagnostic server for Tuner X
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
4. **Multi-adapter USB-CAN** — any number of USB-CAN adapters, classic CAN 1 Mbps like a stock SystemCore (`touch /etc/pi5b-can-fd` opts into CAN FD 1M/5M — FRC devices on a `can_s*` bus are classic, and FD mode made Phoenix's frames bus-off a Talon FX)
5. **Persistent CAN naming** — USB port path mapped to stable can_sN index via `/etc/can_port_map`, works with USB hubs
6. **CAN is optional** — 30s timeout, robot starts regardless of adapter presence
7. **CAN discovery frame** — `cansend 000#00` sent on each bus after interface up
8. **vcan placeholders** — canbusprocess fills missing `can_s0..can_s4` slots with vcan interfaces after USB-CAN setup. HAL aborts the robot program if any of the 5 buses is missing (`SIOCGIFINDEX ... No such device`), so this is required for robot.service to start with fewer than 5 physical adapters.
9. **robot_heartbeat loaded after the buses exist** — the tail of the canbusprocess override, *after* the vcan loop: stop `mrccomm.service`, `rmmod robot_heartbeat`/`can_sender`, delete any regular files left on `/dev/mrccan/*`, `modprobe robot_heartbeat`, start `mrccomm.service`. `can_sender` (robot_heartbeat's dependency) enumerates `can_s*` netdevs in its module init and returns `-ENODEV` (`CAN Sender: No CAN devices found`) when there are none, and it snapshots the ≤5 devices it finds — so loading it any earlier (modules-load.d, or the top of the ExecStart) is a silent no-op on a Pi 5B and no enable heartbeat is ever sent, i.e. motors never enable even though robot code runs. Reloading needs MrcCommDaemon's fds closed, hence the stop/start. Upstream's own unit modprobes it last for the same reason.
10. **/dev/mrccan tmpfile (fallback)** — `/etc/tmpfiles.d/mrccan.conf` creates `/dev/mrccan/` at boot. Only for when `robot_heartbeat` is missing or fails to load: with the bare directory present MrcCommDaemon creates *regular files* and still publishes `/Netcomm/Control/ServerReady`, so robot code starts — but without the module there's no CAN heartbeat. With neither, MrcCommDaemon crash-loops on `Failed to open control data file` and the HAL SIGABRTs the robot program ~10s after start with `Error: Waiting for server ready failed`. `/etc/modules-load.d/systemcore-pi5b.conf` still loads **i2c-dev** at boot (upstream modprobes it from the ExecStart this project replaces).
11. **Wireless regdb** — regulatory.db installed for US WiFi channel support. Skipped when the image already ships `/usr/lib/firmware/regulatory.db` (true since Beta 14).
12. **WLAN0 AP settings unlocked** — dashboard JS patched to allow modifying Access Point config
13. **Fault count reset button** — frontend-only baseline reset added to fault tooltip in dashboard
14. **Camera shim** — `LD_PRELOAD` wrapper around `realpath()` for the four vision servers so a camera plugged straight into a Pi 5B port is accepted (see the USB cameras section)
15. **iodaemon off** — drop-in `limelight_iodaemon.service.d/20-pi5b.conf` with `ConditionPathExists=/etc/pi5b-enable-iodaemon`. On a Pi 5B the RP2350 reports **brownout permanently** (no battery sense), iodaemon publishes it, and `MrcCommDaemon` then never sets the enabled bit in the CAN heartbeat (its log shows `Brownout: 1`, and every CTRE device sits at `Robot Enable: Disabled` in Tuner X). Verified 2026-09-13: with iodaemon stopped the daemon logs `Brownout: 0` and a DS enable produces `Watchdog Enabled / Enabled: 1`, the heartbeat carries enable, and the Talon FXS enables. `sudo touch /etc/pi5b-enable-iodaemon` opts back in.
16. **CAN TX-stall watchdog** — inside the canbusprocess monitor loop: a physical adapter whose `tx_packets` stops growing for 6 s while `rx_packets` keeps growing (the heartbeat module always transmits) is USB de/re-authorized, which runs the normal hot-plug path. Seen on a CANable 2.5 right after boot: 145k frames received, 4 sent, Phoenix saw no devices, and the blocked Phoenix calls stalled the robot loop so the daemon dropped the program's watchdog (`Watchdog Disabled`) seconds after each enable. A bus with no other node is left alone.

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
  (release `limelightosr-2027.0.0-beta14-210`, Sep 2026); the matching alpha is
  `limelightsystemcorecm5-limelightosr-2027.0.0-alpha14.zip` (release `alpha14-380`), built
  from the same source commit — same partition table, same kernel, same dashboard bundle,
  patches identically. Build 210 requires WPILib 2027 Alpha 7 in the robot program.
  Differences 201 → 210 that matter here: config.txt dropped `boot_delay`,
  `dtoverlay=miniuart-bt`, `dtoverlay=pi3-disable-bt` and added `uart_2ndstage=0` (every
  HDMI/SPI-CAN regex still matches); `limelight_picoflasherprocess.service` gained
  `DefaultDependencies=no` + `After=local-fs.target systemd-udevd.service`, which is fine
  for flash-pico.sh (it only needs `udevadm`, `dd` and `/dev/sd*`);
  `limelight_canbuswatchdog.service` gained `After=`/`Wants=limelight_canbusprocess.service`;
  dnsmasq/dnsmasqwifi lost their `[Install]` and wifi DHCP now comes from a new
  `/etc/dhcpcd.exit-hook` (so a "dead" `dnsmasqwifi` is normal); new
  `limelight_supportbundle.service`
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
- Pi 5B USB topology, as observed on hardware running Beta 14 (identical controller
  addresses to the CM5 — same BCM2712 + RP1 silicon):
  - `xhci-hcd.0` (`1f00200000.usb`) → `usb1`/`usb2`: the two blue **USB 3.0** ports. USB-CAN
    adapters landed here as `1-1`, `1-2`.
  - `xhci-hcd.1` (`1f00300000.usb`) → `usb3`/`usb4`: the two black **USB 2.0** ports. Camera on
    `3-1`, RP2350 (`cafe:4011`) on `3-2`.
  - This distinction matters for cameras: only the USB 2.0 ports can produce a `3-x` path.
- Pico after flash: VID=0xCAFE PID=0x4011 ("Limelight RT Subsystem")
- CAN udev match: `ATTR{type}=="280"` (ARPHRD_CAN, matches any CAN netdev). The build's `90-usb-can-rename.rules` adds `SUBSYSTEMS=="usb"` so vcan interfaces (no parent device) don't match — without that constraint, adding a vcan from inside canbusprocess re-triggers canbusprocess and infinite-loops.
- CAN buses are classic CAN 1 Mbps (`fd off`), matching upstream. CAN FD 1M/5M is opt-in via `/etc/pi5b-can-fd`; with it on, a Talon FX on the bus was bus-off'd 19 times in minutes and Tuner X listed no devices (verified 2026-09-12)
- CAN port mapping persisted to `/etc/can_port_map` (port path -> can_sN index)
- HAL CAN expectation: WPILib's HAL (`libwpiHal.so`, `_GLOBAL__N_1::SocketCanState::InitializeBuses`) does `SIOCGIFINDEX` on `can_s0` through `can_s4`. ANY missing one → `IllegalStateException: Failed to initialize. Terminating` from `org.wpilib.framework.RobotBase.startRobot`. canbusprocess fills gaps with vcan after USB-CAN setup.
- HAL netcomm gate: HAL also blocks on NT key `/Netcomm/Control/ServerReady` (string lives in `libwpiHal.so`). The setter is `/usr/bin/MrcCommDaemon` (`mrccomm.service`), which opens `/dev/mrccan/controldata` + `/dev/mrccan/matchinfo` (`O_WRONLY|O_CREAT|O_TRUNC`). Those paths are the misc devices registered by the `robot_heartbeat` module (`kernel/net/can/robot_heartbeat.ko.xz`, depends on `can_sender`) — not carrier-board-specific hardware, but it can only load once a `can_s*` netdev exists: `can_sender`'s init enumerates up to 5 `can_s*` devices, holds references to them, and returns `-ENODEV` (`CAN Sender: No CAN devices found`) if it finds none. The build therefore modprobes it from the **tail of the canbusprocess override**, after USB-CAN renaming and the vcan placeholders, and restarts `mrccomm.service` around it (a reload needs MrcCommDaemon's fds closed; stale *regular* files on `/dev/mrccan/*` are deleted first — `[ -f path ]` is false for a character device, so it only removes fallback leftovers). `/etc/tmpfiles.d/mrccan.conf` remains the fallback: with the bare directory present, MrcCommDaemon creates plain files and still publishes ServerReady, so robot code starts — but nothing sends the CAN enable heartbeat, so motors stay disabled. Without either: `Failed to open control data file` → mrccomm crash-loops → HAL `Waiting for server ready failed` → SIGABRT (exit 134) ~10s after Java starts.
- Dashboard fault chain: RP2350 firmware → (libusb) `iodaemon` → NT `sys/faults` (struct
  `IOFaults`: brownout, io, rsl, usb, display, imu + counts) → `hwmon` (which adds
  `canbus_down`/`canbus_unavail` read from `can_s0..can_s4` itself) → websocket `hw` payload
  (`f` bitmask, `fc` counts) → dashboard. Bit order is
  `[BROWNOUT, I/O, RSL, USB, DISPLAY, IMU, CAN STATUS, CAN AVAIL]`, decoded as `0!==(t&1<<i)`.
- BROWNOUT / I/O / DISPLAY / IMU (bits 0,1,4,5) are permanently faulted on a Pi 5B because the
  closed RP2350 firmware expects carrier-board circuits (battery monitor, Smart I/O short
  detection, display, I2C IMU) that don't exist. They re-fire every poll, so the counts climb
  ~100/s. **BROWNOUT is not cosmetic**: iodaemon publishes `/sys/brownout`, MrcCommDaemon
  reads it (`Brownout: %d` in its control-data dumps) and, like a roboRIO in brownout
  protection, refuses to enable outputs — which is why patch 15 keeps iodaemon off. CAN STATUS (bit 6) is **not** cosmetic — `hwmon` derives it from the
  real `can_s*` state, so it also fires when a USB-CAN adapter genuinely drops, and it is
  expected while most buses are vcan placeholders. Suppressing bits 0,1,4,5 means masking with
  `0xCC`; leave bit 6 visible or you lose a working diagnostic. `iodaemon` has no config or
  CLI switch to quiet them; masking `limelight_iodaemon.service` kills bits 0-5 at the source
  (and also the IMU / Smart I/O topics, none of which exist on a Pi 5B).
- Dashboard patches: `patcher/dashboard.py`, shared verbatim by both patchers, applied to every rootfs slot present (`main.*.js` is `main.bb62576c.js` in 210/380). **No minified identifier may be hardcoded** — upstream re-minifies every build:
  - jsx-runtime alias for the fault button: `xo` (Beta 10), `bo` (201), `Ro` (210/380); a wrong one throws a ReferenceError and blanks the dashboard.
  - WLAN0 AP unlock: `renderInterfaceConfig=function(e){var …}` is located, the locals bound to `"wlan0"===e` and `<this>.state.waiting||…` are read out of its head, and `disabled:<busy>||<wlan>` is replaced with `disabled:<busy>` inside that function's span only (it ends at the next `,<this>.<name>=function`). 7 sites in 201, 210 and 380; the 8th, `disabled:<busy>||<eth0>`, must survive. The old hardcoded `disabled:o||a` sed matched nothing in 210 — silently, which is why zero-match dashboard seds now log WARNING and `--validate` re-checks the bundle.
  - `--validate` fails if `window.__faultBL=window.__rawFC` or `window.__rawFC=t.fc` is absent, the `,{static_ip:"172.30.0.1",…}` literal survives, or any wlan lock remains, and runs `node --check` when `node` is on PATH (sudo usually drops nvm's PATH — `sudo -E env PATH=$PATH …` to keep it).
- Interface renaming done in canbusprocess service (NOT udev PROGRAM — `ip link show` is unreliable in udev context)
- Systemd ExecStart must not use `${VAR##pattern}` syntax — systemd strips `${...}` before bash sees it
- `patcher_win` (the no-WSL path) writes partitions directly, and three of its primitives were silently wrong until they were exercised end-to-end against Beta 14. Re-check these if that code is touched: FAT lookups must be case-insensitive (pyfatfs reports 8.3 entries upper-cased, so `/config.txt` misses `CONFIG.TXT`); pyfatfs closes the stream handed to it, so the buffer must survive `close()` for the write-back; and `debugfs` needs `set_inode_field <path> mode 0100644` (with the S_IFREG bits) plus a guard against `mkdir` on paths that already exist, or the result is a filesystem that fails `e2fsck` and throws EIO on readdir. Validate any change with `e2fsck -fn` on the extracted rootfs — the patcher's own `--validate` passed on an image the kernel refused to read.
- Upstream ships `limelight_canbusprocess.service` as `Type=oneshot`/`RemainAfterExit=yes` (Beta 14). Our drop-in's ExecStart never returns and sets `Restart=always`, which systemd refuses on a oneshot unit ("isn't allowed for Type=oneshot services. Refusing.") — so the drop-in also sets `Type=simple`/`RemainAfterExit=no`. Check drop-ins against a new release with `systemd-analyze verify <unit>` before flashing.
- Upstream `robot.service` (Beta 14) waits up to 15s for `can_s0..can_s4` to be `state UP` and starts anyway on timeout; our override replaces that ExecStartPre with a 30s wait for all five `/sys/class/net/can_s0..can_s4` to **exist** (a vcan placeholder is `state UNKNOWN` and never comes UP, so upstream's UP test would always burn the full timeout), then starts regardless
- Hot-plug (verified 2026-09-12 by de/re-authorizing the USB device via sysfs): unplug → `can_sN` vanishes → monitor loop exits → restart stops the `/dev/mrccan` holders — mrccomm, expansionhubdaemon, limelight_iodaemon **and robot.service** (the HAL opens `controldataro` + `enabledro`; with it running `rmmod` fails and the reload silently degrades to "still loaded from an earlier run") — unloads `robot_heartbeat`/`can_sender` (releasing the netdev ref that would otherwise block the kernel's unregister forever), fills the slot with a vcan, reloads the module and starts the holders again (robot last). Replug → `90-usb-can-rename.rules` restarts the unit → the vcan in that slot is deleted, the adapter renamed back, same reload cycle; the robot program therefore restarts on every adapter change, which it needs anyway (a re-enumerated adapter has a new ifindex and the HAL's sockets can't follow). `/run/pi5b-can-physical` only feeds a log line. Placeholders otherwise persist across restarts so the HAL's vcan sockets stay valid; the wait-for-adapters loop is satisfied by an existing vcan, so restarts don't burn the 30 s boot-time wait. `/run/pi5b-can-holders` remembers which daemons were stopped so a restart that interrupts a reload still brings them back. Camera unplug/replug needs nothing: visionserver drops and re-finds it on its own rescan.
- `ip link set <if> type can ...` applies attributes in order and aborts at the first the driver rejects, keeping the earlier ones. gs_usb returns EOPNOTSUPP for `restart-ms`, so a trailing `restart-ms 1000` left `fd on` set with no data bit-timing and `ip link set up` failed with `incorrect/missing data bit-timing` (seen on a CANable 2.5). Never append attributes gs_usb doesn't support, and the classic-CAN fallback must say `fd off` explicitly. Verified on hardware: CANable 2.5 (`1d50:606f`, 160 MHz clock) runs CAN FD 1M/5M; the older candleLight falls back to classic CAN
- Upstream `70-can-interface-names.rules` names the carrier board's SPI CAN controllers (`spi2.0` → `can_s0`, etc.). It never matches on Pi 5B since the SPI overlays are commented out, so it doesn't conflict with `90-usb-can-rename.rules`.

- Phoenix / CTRE on SystemCore: the Phoenix diagnostic server that Tuner X talks to is part
  of the robot program (Phoenix 6 vendordep), so there is nothing to pre-install; Tuner X
  needs a deployed program that constructs a Phoenix 6 device. `examples/phoenix-tuner-robot/`
  is that program (WPILib/GradleRIO **2027.0.0-alpha-7** + Phoenix 26.50.0-alpha-1 with the
  vendordep's `wpilibYear` overridden to `2027_alpha7`, because CTRE has no Alpha 7 build yet;
  GradleRIO otherwise refuses it). **Alpha 7 is mandatory on build 210, not just recommended:**
  `MrcCommDaemon` (210) only writes bit 0 (enabled) into `/dev/mrccan/controldata` — and so
  into the CAN heartbeat — while the program feeds `/Netcomm/Control/WatchdogActive`, an NT
  topic the Alpha 7 HAL publishes and the Alpha 6 HAL does not (`strings` on both binaries).
  An Alpha 6 program therefore runs, sees itself enabled, but Tuner X shows every device
  `Robot Enable: Disabled` and the daemon E-stops. Both HALs otherwise start fine on 210:
  NT on 5810, `[phoenix-diagnostics] Server 2026.50.0 ... running on port: 1250`, answers
  `http://<pi>:1250/?action=getversion` with `"System":"Systemcore"`. Build with
  `JAVA_HOME=~/wpilib/2027/jdk` (JDK 25), deploy with `./gradlew deploy -PsystemcoreHost=<ip>`
  (GradleRIO's SystemCore target: user/password `systemcore`, jar → `/home/systemcore/wpilib/
  classpath`, JNI libs → `/home/systemcore/wpilib/third-party/lib`, writes `robotCommand`,
  enables+starts `robot.service`). Alpha 7 template differences: `application` plugin,
  `debugJni` lives on the WPILibJavaArtifact, `RobotBase.startRobot(Robot::new)`,
  `org.wpilib.driverstation.RobotState.isEnabled()` (no `DriverStation.isEnabled`). The
  heartbeat module's control word (`controldata_store`, hex text): bit 0 enabled, bits 1-4
  mode flags, `/dev/mrccan/enabledro` reads back `0`/`1`, `controldataro` the last word
  (`1000510` = disabled).
  The Pi's sshd (OpenSSH ≥ 9.8, `PerSourcePenalties`) answers `Not allowed at this time` for
  ~5–10 min after a burst of connection probes (e.g. `nc -z` on port 22) — wait or reboot. Phoenix's default SystemCore
  bus is `can_s1` (`CANBus.systemCore(n)` selects `can_s<n>`). CTRE's dashboard-installable
  CANivore packages (`https://ctre.download/files/systemcore/canivore-usb-kernel_1.18_aarch64.ipk`
  + `canivore-usb_1.16_aarch64.ipk`) do not work on build 210: the module was built against an
  earlier Beta 14 kernel and fails with `disagrees about version of symbol module_layout`
  (CRCs differ from the 210 tree's `Module.symvers`). Rebuilding it needs the 210 kernel
  tree + toolchain from the release assets (`systemcorebetalinux.tar.xz` is a fully built
  tree with `.config` and `Module.symvers`; `crosscomp_examples/kernel` in the upstream repo
  shows the flow) and CTRE's driver source, of which only `canivore-usb_1.8_arm64.deb`
  (DKMS, 2022, `canivore-usb-util.o` shipped as a binary) is public. Deliberately not done —
  the project targets plain USB-CAN adapters. The image installs add-on packages with `opkg`
  (`/var/lib/opkg/status`, dashboard `/api/packages/*` on port 4803).

## USB cameras on a bare Pi 5B (fixed by the camera shim, confirmed on hardware)

Each `visionserverN` canonicalises `/sys/class/video4linux/videoN/device` with `realpath()`
(imported as `__realpath_chk` — the binaries are fortified) and compares the result against
one hard-coded path that includes the **carrier board's internal 4-port USB hub**:

```
camera plugged straight in : .../xhci-hcd.1/usb3/3-1/3-1:1.0          (84 bytes)
what visionserver expects  : .../xhci-hcd.1/usb3/3-1/3-1.1/3-1.1:1.0  (92 bytes)
```

Without help every server logs `Camera initialization failed (no camera on this port)`
even though the kernel is happy (`uvcvideo` loaded, `/dev/video0` present).

**Fix (patch 14):** `patcher/resources/camera-shim/pi5b-camera-shim.so`, loaded into the
four vision servers via `LD_PRELOAD` (drop-in `20-pi5b-camera.conf`), wraps `realpath` /
`__realpath_chk` / `canonicalize_file_name` and presents a camera that sits directly on a
root port under the hub path the binaries expect. Nothing else is rewritten — strace shows
the servers never open anything under the canonical path. Cameras already behind a hub
(`3-1.2/3-1.2:1.0`) pass through untouched, so the hub setup keeps working. Verified on
hardware: a UVC camera on the black port `3-1` is picked up as
`found camera at .../3-1/3-1.1/3-1.1:1.0` → `USB camera generic-UVC ... locked to
1280x720@30 MJPG`. The shim logs one line, `pi5b-camera-shim: presenting <real> as <fake>`,
in each server's journal. Unplug/replug needs nothing extra: the server drops the camera and its
periodic rescan re-finds it within seconds (verified via sysfs `authorized` toggling).

Physical port → virtual hub port → which instance claims it (`usb_id` lives in
`/usr/local/bin/visionserverN/global.settings`, settable via the dashboard `set_usb_id` API;
all four ship with `usb_id: 0`, so they all target `3-1.1` and the first to claim it wins —
the others log "no camera on this port", which is expected):

| Pi 5B port | presented as | `usb_id: 0` (any server) | `usb_id: 1` |
| --- | --- | --- | --- |
| `3-1` USB 2.0 (black) | `3-1.1` | yes | — |
| `3-2` USB 2.0 (black) | `3-1.2` | — | `visionserver1` |
| `1-1` / `2-1` USB 3.0 (blue) | `3-1.3` | — | `visionserver2` |
| `1-2` / `2-2` USB 3.0 (blue) | `3-1.4` | — | `visionserver3` |

Override the table with `Environment=PI5B_CAMERA_PORTS=3-1=1,1-1=3,...` in the drop-in.
Rebuild the `.so` with `patcher/resources/camera-shim/build.sh <toolchain dir>` (the
SystemCore release toolchain; Ubuntu's `gcc-aarch64-linux-gnu` also works).

Do **not** try to fix this by patching the path string in the binary: it was tested, and the
comparison length is baked in, so the shorter Pi 5B path plus NUL padding still fails to
match. Making that work would mean rewriting the length immediate in the instruction stream.

A camera that isn't in Limelight's database comes up as `generic-UVC` (logged as
`USB camera generic-UVC (...): locked to 1280x720@30 MJPG`) — it streams, but there is no
factory calibration or lens profile, so calibrate before trusting pose estimates.

Useful commands when a camera doesn't appear:

```bash
lsusb                                              # is it enumerated at all?
lsmod | grep uvcvideo                              # driver loaded?
readlink -f /sys/class/video4linux/video0/device   # the real path (before the shim)
journalctl -u limelight_visionserver -n 50 --no-pager | grep -iE "shim|camera"
```

Note this Buildroot userspace has **no `timeout`, `pkill` or `pgrep`** — use `killall`/`pidof`
and the background-then-sleep-then-kill pattern. `strace` is present (`ltrace` is too but
could not hook this binary's PLT); tracing `%file` is how the path comparison above was
identified, and `readelf --dyn-syms` is how the fortified entry point was found.

## Diagnosing a non-starting robot.service on an already-flashed image

```bash
sudo journalctl -u robot.service -n 50 --no-pager
```

| Symptom (journal line) | Cause | Fix |
| --- | --- | --- |
| `ioctl(SIOCGIFINDEX) for CAN can_sN failed with No such device` then `Failed to initialize. Terminating` | Slot `can_sN` is missing from `/sys/class/net/` (fewer than 5 USB CAN adapters and the vcan-placeholder logic isn't running) | `sudo modprobe vcan && sudo ip link add dev can_sN type vcan && sudo ip link set can_sN up` for each missing N. If a service is deleting them, check `/etc/udev/rules.d/90-usb-can-rename.rules` — must include `SUBSYSTEMS=="usb"`, else `ip link add` of a vcan re-triggers canbusprocess which has a "delete CAN interfaces without `device/driver`" cleanup that wipes the vcan you just made. |
| `Error: Waiting for server ready failed. Restarting app and retrying...` then `terminate called without an active exception` and `Aborted (core dumped)` (exit 134) | `MrcCommDaemon` isn't setting `/Netcomm/Control/ServerReady` in NT4 — almost always because it's crash-looping on `Failed to open control data file` | `robot_heartbeat` only loads once at least one `can_s*` exists, so bring the buses up first, then: `sudo systemctl stop mrccomm.service; sudo rm -f /dev/mrccan/*; sudo modprobe robot_heartbeat && sudo systemctl start mrccomm.service`. `CAN Sender: No CAN devices found` in dmesg means there were no `can_s*` yet — check `limelight_canbusprocess`, which does this whole sequence after its vcan loop. If the module really is unavailable, `sudo mkdir -p /dev/mrccan && sudo systemctl restart mrccomm.service` gets robot code running but with **no CAN enable heartbeat** (motors stay disabled). `ls -l /dev/mrccan`: character devices = module loaded, regular files = tmpfile fallback (delete them before reloading the module). |
| `Failed to initialize can buses` for `can_d2` (not `can_s2`) | Both `can_s*` AND `can_d*` are probed by the HAL. `can_d0..can_d19` are typically created by stock SystemCore-OS init; if they're missing the image is broken — don't rename `can_d*` interfaces away. | Reboot to let stock init recreate them, or `sudo modprobe vcan && for i in $(seq 0 19); do sudo ip link add dev can_d$i type vcan; sudo ip link set can_d$i up; done` |

## Dev environment

- Host: WSL2 on Windows (Ubuntu/Debian)
- Target: Raspberry Pi 5 Model B (BCM2712), ARM64
- SystemCore version: 2027.0.0 Beta 14 (limelightosr-2027.0.0-beta14-210; `-201` was deleted upstream on 2026-09-02)
- Confirmed on hardware (Pi 5B, Beta 14 image built by this repo, Aug 2026): boots from SD;
  `limelight_expandfs` grew rootfs A to 6.8G (5.1G free); two USB-CAN adapters enumerate on
  `1-1`/`1-2`; RP2350 present as `cafe:4011` on `3-2`; USB camera works on a bare port with the
  camera shim (v4) and behind a hub without it.
  That image (v1, build 201) loaded `robot_heartbeat` too early to work — see patch 9.
  Verified 2026-09-13 with a Driver Station and the WPILib Alpha 7 example program: `ls -l
  /dev/mrccan` shows character devices, the daemon logs `Enabled: 1 / WatchdogActive: 1 /
  Brownout: 0` on enable, and the Talon FXS (ID 2, can_s0) shows `Robot Enable: Enabled` in
  Tuner X. Bench bus: CANable 2.5 on `1-1`, Talon FXS 2, Pigeon 2 (7), PDP 62, CANdle 0.
- Test Pi: `ssh systemcore@172.30.0.1` (password: systemcore) — the wlan0 AP address, reachable
  when the host is joined to the Pi's access point. Older notes list 10.0.0.167/10.0.0.169;
  those were LAN leases and are stale.

## Network boot (for development)

WSL2 serves TFTP + NFS to Pi 5 over the LAN. Requires mirrored networking mode in `.wslconfig`. Run `netboot/setup-netboot.sh` to configure, then set Pi EEPROM boot order to `0xf21`.

**Dead as of build 210**: that kernel moved the NFS client from built-in to a module and
dropped `nfsroot=`/`Root-NFS` support entirely, so `root=/dev/nfs` cannot mount without an
initramfs. Netboot still works with Beta 10-13 / `beta14-201` kernels; for 210+, flash SD.

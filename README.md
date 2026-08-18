# FRC SystemCore on Raspberry Pi 5

Run [Limelight SystemCore OS](https://github.com/LimelightVision/systemcore-os-public) on a standard Raspberry Pi 5 Model B instead of the Compute Module 5 it was designed for.

## Quick Start

```bash
git clone https://github.com/netarcx/systemcore-rpi5-guide.git
cd systemcore-rpi5-guide
sudo ./build-image.sh
```

This produces `systemcore-pi5b-2027.0.0-beta14-v1.img` — flash it to an SD card and boot:

```bash
sudo dd if=systemcore-pi5b-2027.0.0-beta14-v1.img of=/dev/sdX bs=4M status=progress
```

Insert the SD card into your Pi 5 and power on. No further configuration needed.
On first boot the stock `limelight_expandfs` service grows rootfs A to 7 GiB and
creates the (empty) B slot — that's upstream behavior, not something this project adds.

To build against a different upstream release, edit `RELEASE_TAG` / `RELEASE_NAME`
at the top of `build-image.sh`, or just point `patch-image.py` at any downloaded
image (see below).

## Patching new upstream releases (`patch-image.py`)

When WPI/Limelight ships a new SystemCore image, you can patch it directly without re-running the full build:

```bash
# GUI:
sudo python3 patch-image.py

# Or headless:
sudo python3 patch-image.py upstream.img -o patched.img
```

The patcher detects partition offsets dynamically (via `sfdisk`), so it survives layout changes between releases. It applies the same set of patches `build-image.sh` does, but skips the image download. It accepts either the release `.zip` or an already-extracted `.img`.

### Image families

| Family | Releases | Layout |
| --- | --- | --- |
| `ab` | Beta 10+ and Alpha 11+ | boot selector + boot A/B + rootfs A/B |
| `alpha` | Alpha 2-10 | single boot partition + rootfs A/B + data |

Auto-detected from the partition table; override with `--image-type` if needed.

Since Beta 14 / Alpha 14 the images ship **packed**: rootfs A is shrunk to its filesystem
size and rootfs B is declared in the partition table but isn't in the file at all. The
patcher notices this, patches rootfs A, and says so in the log. Slot B is created empty on
first boot by the stock `expandfs.sh` and is only ever filled by an OTA update, which
overwrites the whole slot — so there is nothing there to patch, and an OTA to slot B drops
these patches by design.

### CLI options

```
sudo python3 patch-image.py [input.img] [options]

  -o, --output PATH         Output image (default: <input>-pi5b.img)
  --dry-run                 Log everything but don't modify anything
  -v, --verbose             Show every shell command
  --backup                  Copy input to .bak before in-place patching
  --keep-mounted            Don't unmount partitions on success
  --no-cleanup-on-error     Leave mounts open if a patch fails (for debugging)
  --skip-b                  Patch A partitions only, skip B
  --validate                Re-mount the output and verify expected files
  --inspect                 Mount partitions, print paths, wait for ENTER
  --show-partitions         Print partition layout and exit
  --image-type TYPE         Force image family: auto (default), ab, or alpha
  --list-patches            List every patch with a description
  --only PATCH,PATCH        Apply only these patches (everything else off)
  --no-<patch>              Skip a single patch (e.g. --no-install-mrccan)
```

Run `sudo python3 patch-image.py --list-patches` for the full set of patch names.

### GUI

The GUI is a Tkinter app (`apt install python3-tk` if missing). Launch with no arguments to bring up the window, or pass `--gui` to force GUI even when positional args are given.

```bash
sudo python3 patch-image.py
```

The window is laid out top-to-bottom in seven sections:

**1. Image files** — pickers for the input image and the output image. Selecting an input auto-fills the output as `<input>-pi5b.img` next to it (override with the second Browse button).

**2. Source paths** — auto-filled from the repo:
- `flash-pico.sh`: path to the Pico flasher script that gets installed into `/usr/local/bin/`
- `wireless-regdb .deb`: path to the regdb Debian package extracted into `/usr/lib/firmware/`

Each path has a Browse button if the default isn't right.

**3. Boot partition patches (A + B)** — checkboxes for the boot-side patches:
- `Enable HDMI` — uncomment the display options in `config.txt`
- `Disable SPI CAN overlays` — comment out `dtoverlay=spi*` / `dtoverlay=sc-mcp2518` lines (no SPI CAN hardware on Pi 5B)
- `Add panic=0 + US wifi regdom` — append kernel cmdline params

**4. Rootfs patches (A + B)** — checkboxes for the rootfs-side patches:
- `Install flash-pico.sh` — install the script + service override that lets external Picos be flashed
- `USB-CAN udev rule` — install `90-usb-can-rename.rules` (scoped to USB so vcan placeholders don't trigger restart loops)
- `canbusprocess override (vcan placeholders)` — install the override that names USB-CAN adapters and fills any missing `can_s0..can_s4` slot with a vcan interface (HAL requires all 5)
- `canbuswatchdog override` — install watchdog override that waits for any `can_s*` instead of requiring all 5
- `robot.service override` — 30-second CAN wait then start regardless
- `/dev/mrccan tmpfile (MrcCommDaemon fix)` — install `tmpfiles.d/mrccan.conf` so `MrcCommDaemon` can write its control files at boot (without this the robot program SIGABRTs ~10s after start)
- `Load robot_heartbeat + i2c-dev at boot` — install `modules-load.d/systemcore-pi5b.conf`. `robot_heartbeat` registers the real `/dev/mrccan/*` devices; upstream loads it from the CAN service ExecStart this project overrides
- `Wireless regulatory database` — install `regulatory.db` so WiFi works on the US regdom
- `Dashboard: unlock WLAN0 AP` — patch the minified React JS to allow editing Access Point network config
- `Dashboard: fault count reset button` — add a "Reset Fault Counts" button to the fault tooltip in the header

Hover any checkbox for a tooltip explaining what that patch does.

**5. Debug + advanced** — toggles that change *how* patching runs rather than *what* it does:
- `Dry run (log only)` — log every operation but don't write anything to the image
- `Verbose log` — show every shell command (sfdisk, mount, dpkg-deb, etc.)
- `Backup input image` — copy input to `<input>.bak` before in-place patching (no-op if input ≠ output)
- `Keep mounted after` — leave the partition loop-mounts open on success so you can poke around with a file manager or shell. Cleanup is your problem.
- `No cleanup on error` — leave the loop-mounts open if a patch fails, so you can inspect partial state. Use for diagnosing why a patch broke.
- `Patch A only (skip B)` — only patches the A boot+rootfs, leaves B alone. Useful when testing a patch against just one boot slot.
- `Validate after patch` — re-mount the output image and verify the expected files (override.conf paths, tmpfile config, flash-pico.sh) are present in every rootfs slot the image actually contains.

**6. Action buttons:**
- `Patch image` — apply all enabled patches. Disabled while a job is running.
- `Inspect (mount only)` — mount every partition present in the image (boot A/B, rootfs A/B) on the input or output image and show a dialog with the mount points. Click OK in the dialog when done to unmount everything.
- `Validate` — re-mount the output (or input) image and verify expected post-patch files are present. Pops up a dialog reporting any missing files.
- `Show partitions` — print the partition layout (offsets, sizes, detected filesystems, identified boot/root A/B mapping) to the log.
- `Cancel` — set a cancel flag the worker can observe. Subprocess calls already in flight aren't interrupted, so this is "best effort" rather than instant.
- `Quit` — close the window. Doesn't unmount anything — if you'd been using Keep mounted or No cleanup on error, run `umount` manually first.

**7. Progress bar + status + log viewer** — the bottom half is a scrolling log with timestamps. Errors render red, warnings amber, debug messages grey. `Clear` empties the log; `Save log...` writes it to a file (handy for sharing diagnostics).

### Windows-native patcher (`patch-image-win.py`)

For patching without WSL. Same patches, but it reads/writes the partitions directly instead of
mounting them: `pyfatfs` for the FAT32 boot partitions, `debugfs` (e2fsprogs) for the ext4 rootfs.

```
pip install pyfatfs setuptools     # setuptools: pyfilesystem2 still imports pkg_resources
python patch-image-win.py                       # GUI
python patch-image-win.py image.zip -o out.img --validate
```

Place `debugfs.exe` in `patcher_win/tools/` — see the README there for where to get it.

Verify the result with `e2fsck -fn` on the rootfs partition if you want extra confidence; a
correct run reports no errors.

### USB cameras need a USB hub

A camera plugged straight into the Pi 5B enumerates fine but never appears on the dashboard's
camera page — the vision servers match cameras against a sysfs path that includes the Limelight
carrier board's internal USB hub, which a Pi 5B doesn't have.

**Plug a USB hub into one of the black USB 2.0 ports and the camera into the hub.** The path
then matches what the stock software expects, with no patching. The blue USB 3.0 ports are on a
different controller and won't work for this. See CLAUDE.md for the port-to-instance mapping if
you want more than one camera.

## What `build-image.sh` does

The script automates everything needed to convert the upstream CM5 image into a Pi 5B-compatible image:

1. **Downloads** the upstream SystemCore image from GitHub (cached after first download)
2. **Hands it to `patch-image.py`**, which finds the partitions with `sfdisk` — offsets move
   between releases, so nothing is hardcoded — and applies every patch
3. **Patches both boot partitions** (A/B) — enables HDMI, disables SPI CAN overlays, updates cmdline
4. **Patches every rootfs slot present in the image** — installs Pico flasher, CAN adapter
   support, the `/dev/mrccan` devices, and the dashboard patches

Patch definitions live in `patcher/` and `patcher/resources/`, so `build-image.sh` and
`patch-image.py` cannot drift apart.

## What gets patched

### HDMI output

The stock image disables all display output (headless for Limelight hardware). The build script enables HDMI for debugging by commenting out `hdmi_ignore_hotplug`, `hdmi_blanking`, `ignore_lcd` and setting `display_auto_detect=1`.

### SPI CAN overlays disabled

The CM5 carrier board has 5 MCP2518FD SPI CAN controllers. The Pi 5B has none of this hardware, so the SPI CAN overlay lines are commented out.

### Pico flasher (`flash-pico.sh`)

The stock `picoflasherprocess` binary only flashes Pico microcontrollers connected to the Limelight's internal USB controller. On Pi 5B, external Picos are rejected.

The build script replaces it with `flash-pico.sh` via a systemd override. This script:
- Polls for any Pico in BOOTSEL mode on any USB port (vendor ID `2e8a`)
- Flashes `fw.uf2` via `dd` to the raw block device
- Works with RP2350 Picos (RP2040 not supported — firmware is RP2350-specific)
- After flashing, the Pico appears as `cafe:4011` ("Limelight RT Subsystem")

### Multi-adapter USB-CAN support (optional)

The stock image expects 5 SPI CAN interfaces (`can_s0` through `can_s4`). The build script adds support for any number of USB-to-CAN adapters:

- **Udev rule** triggers the CAN service restart when an adapter is plugged in. The match is scoped to `SUBSYSTEMS=="usb"` so vcan placeholders (see below) don't re-trigger the service and cause an infinite restart loop.
- **canbusprocess override** discovers all CAN interfaces, renames them to `can_s0`, `can_s1`, etc., configures each with CAN FD (1Mbps/5Mbps) or falls back to standard CAN (1Mbps)
- **Persistent port mapping** — each USB port path is mapped to a stable `can_sN` index in `/etc/can_port_map`, so the same physical port always gets the same name regardless of plug order (works with USB hubs)
- **Discovery frame** — `cansend 000#00` sent on each bus after interface up
- **Hot-plug** — plugging in a new adapter triggers automatic naming and configuration
- **vcan placeholders auto-fill missing buses** — the WPILib HAL iterates `can_s0` through `can_s4` and aborts the robot program if any are missing (`ioctl(SIOCGIFINDEX) for CAN can_sN failed with No such device` → `Failed to initialize. Terminating`). After USB-CAN setup, the service creates vcan interfaces for whichever slots have no physical adapter, so 0–4 USB adapters all work.
- **canbuswatchdog/robot.service overrides** — wait for any CAN adapter, start regardless after 30s

If no CAN adapter is plugged in, all services time out gracefully and the robot starts anyway (vcan placeholders satisfy the HAL).

Compatible with any SocketCAN-supported USB adapter (candleLight/canable, PEAK, EMS, etc.). Mixed CAN FD and standard CAN adapters work together.

### MrcCommDaemon unblock (`/dev/mrccan/`)

`MrcCommDaemon` is the userspace service that sets the NetworkTables key `/Netcomm/Control/ServerReady`. The WPILib HAL waits on this key during robot startup — if `MrcCommDaemon` isn't running, the Java robot program SIGABRTs ~10 seconds after launch with `Error: Waiting for server ready failed. Restarting app and retrying...` and `terminate called without an active exception`.

The daemon writes its state to `/dev/mrccan/controldata` and `/dev/mrccan/matchinfo`. Those are misc devices registered by the `robot_heartbeat` kernel module (which pulls in `can_sender`) — both ship in the image and are plain software modules, so they load fine on a Pi 5B. The catch: upstream only loads them from the tail of `limelight_canbusprocess.service`'s ExecStart, which this project replaces wholesale to drive USB-CAN adapters. Without the modprobe the devices never appear and the daemon crash-loops with `Failed to open control data file`.

So the build does both:

- installs `/etc/modules-load.d/systemcore-pi5b.conf` (`robot_heartbeat`, `i2c-dev`) so the devices are registered early in boot, before `mrccomm.service` starts
- keeps `/etc/tmpfiles.d/mrccan.conf` as a fallback — with the bare `/dev/mrccan/` directory present, the daemon creates plain files there and still publishes `ServerReady`

### Dashboard patches

The build script patches the Limelight dashboard (minified React JS) to:

- **Unlock WLAN0 AP settings** — the stock dashboard disables editing Access Point network config. The patch removes the disabled flag and the forced IP/gateway overrides on save.
- **Fault count reset button** — adds a "Reset Fault Counts" button to the fault tooltip in the header. Uses a frontend-only baseline offset (no backend changes needed for the closed-source `diagnosticsprocess`).

### Wireless regulatory database

The stock image is missing `regulatory.db`. The build script installs the US regulatory database so WiFi works correctly (paired with `cfg80211.ieee80211_regdom=US` in the kernel cmdline).

## Known limitations

- **RP2350 firmware faults** — After flashing, the Pico firmware reports faults for hardware it expects on the carrier board (BROWNOUT, IMU, DISPLAY, CAN, RSL). These are cosmetic — USB communication works fine. The firmware is closed-source so these cannot be fixed. Use the "Reset Fault Counts" button to clear them.
- **RP2040 not supported** — `fw.uf2` is RP2350-specific. An RP2040 Pico will accept the copy but reboot back to BOOTSEL in a loop.
- **USB gadget mode** — The `dwc2` overlay behavior may differ between CM5 and Pi 5B.

## Project layout

```
build-image.sh          - End-to-end image builder (run with sudo)
patch-image.py          - Standalone patcher for new upstream releases (GUI + CLI)
patcher/                - Python package for patch-image.py
  core.py               - Mount + per-patch logic + orchestrator
  gui.py                - Tkinter GUI
  cli.py                - argparse entry point
  resources/            - Drop-in files installed into the rootfs
    90-usb-can-rename.rules
    canbusprocess-override.conf
    canbuswatchdog-override.conf
    robot-override.conf
    picoflasher-override.conf
    mrccan.conf
    modules-load.conf
netboot/                - Network boot setup (development/debugging)
  flash-pico.sh         - Pico flasher replacement (installed into image)
  setup-netboot.sh      - Sets up TFTP + NFS on WSL2 for netboot
  cmdline_nfs.txt       - Kernel cmdline template for NFS root
```

Files not tracked in git (generated/downloaded):
```
cache/                            - Downloaded upstream image zip
systemcore-pi5b-2027.0.0-beta14-v1.img   - Output image (~2.2GB, expands to 14GB on first boot)
netboot/tftpboot/                 - TFTP boot files (kernel, DTBs, overlays)
netboot/nfsroot/                  - NFS root mount point
```

## Network boot (development)

For iterative development, the Pi 5 can netboot from a WSL2 host via TFTP + NFS:

```bash
sudo ./netboot/setup-netboot.sh
```

This installs `tftpd-hpa` and `nfs-kernel-server`, configures exports, and prints Pi 5 EEPROM settings. WSL2 must use **mirrored networking** mode (set `networkingMode=mirrored` in `%USERPROFILE%\.wslconfig`).

## Prerequisites

- Linux host for building (Ubuntu/Debian, WSL2 works)
- ~6GB free disk space for the current packed images (upstream zip + extracted + patched
  output); older, full-size releases need ~35GB
- `sudo` access (for loop-mounting image partitions)

## Tested on

- Raspberry Pi 5 Model B (4GB/8GB)
- SystemCore 2027.0.0 Beta 14 (`limelightosr-2027.0.0-beta14-201`) and Alpha 14
  (`limelightosr-2027.0.0-alpha14-371`) — both ship the same partition layout and patch identically
- Also supports Beta 10-13 and the older Alpha 2-10 single-boot-partition layout
- Kernel: stock upstream 16K-page kernel, 6.12.77 (works on Pi 5B as-is since Beta 10)
- Host: WSL2 on Windows 11

## License

The kernel is licensed under GPL-2.0 (same as the Linux kernel). Boot configuration files and scripts are provided as-is.

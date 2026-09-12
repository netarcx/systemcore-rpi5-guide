#!/bin/bash
# Build a ready-to-flash SystemCore image for the Raspberry Pi 5 Model B.
#
# Downloads the upstream Limelight SystemCore release, then hands it to
# patch-image.py, which owns every patch. The patch definitions live in
# patcher/ and patcher/resources/ so there is exactly one copy of each.
set -euo pipefail

PI5B_VERSION="v2"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Upstream release. Both the beta and alpha lines ship the same partition
# layout since Alpha 11 and patch identically — to build from an alpha release
# instead, change the tag/name below and swap IMAGE_ZIP_NAME's prefix to
# "limelightsystemcorecm5-".
RELEASE_TAG="limelightosr-2027.0.0-beta14-210"
RELEASE_NAME="limelightosr-2027.0.0-beta14"
IMAGE_ZIP_NAME="limelightsystemcorebetacm5-${RELEASE_NAME}.zip"
IMAGE_URL="https://github.com/LimelightVision/systemcore-os-public/releases/download/${RELEASE_TAG}/${IMAGE_ZIP_NAME}"

# Cache per release TAG, not per zip name: upstream reuses the zip name across
# rebuilds of the same release (beta14-201 was deleted and replaced by
# beta14-210 with an identical filename), so a name-keyed cache would serve a
# stale image forever after a tag bump.
IMAGE_ZIP="${SCRIPT_DIR}/cache/${RELEASE_TAG}/${IMAGE_ZIP_NAME}"
OUTPUT_IMG="${SCRIPT_DIR}/systemcore-pi5b-${RELEASE_NAME#limelightosr-}-${PI5B_VERSION}.img"

# --- Step 1: Preflight ---

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Must run as root (sudo $0)"
    exit 1
fi

for cmd in wget python3 sfdisk mount umount; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: Required tool not found: $cmd"
        exit 1
    fi
done

if [ ! -f "${SCRIPT_DIR}/netboot/flash-pico.sh" ]; then
    echo "ERROR: netboot/flash-pico.sh not found"
    exit 1
fi

echo "=== SystemCore Pi 5B Image Builder (${RELEASE_NAME}) ==="
echo ""

# --- Step 2: Download upstream image ---
# NOTE: Beta 10+ ships a 16K-page kernel with matching userspace, and the boot
# partitions ship a Pi 5 Model B device tree. We no longer replace either.

mkdir -p "$(dirname "$IMAGE_ZIP")"

if [ ! -f "$IMAGE_ZIP" ]; then
    echo "[1/3] Downloading upstream SystemCore image (${RELEASE_NAME})..."
    wget -c -O "$IMAGE_ZIP" "$IMAGE_URL"
else
    echo "[1/3] Upstream image already cached: $(basename "$IMAGE_ZIP")"
fi

# --- Step 3: Patch ---
# patch-image.py extracts the .img from the zip, finds the partitions with
# sfdisk (offsets move between releases, so nothing is hardcoded), and applies
# every patch to both boot partitions and to each rootfs present in the image.

echo "[2/3] Patching image..."
rm -f "$OUTPUT_IMG"
python3 "${SCRIPT_DIR}/patch-image.py" "$IMAGE_ZIP" \
    --output "$OUTPUT_IMG" \
    --validate

# --- Step 4: Done ---

echo "[3/3] Done!"
echo ""
echo "============================================"
echo "  SystemCore Pi 5B image ready! (${RELEASE_NAME} ${PI5B_VERSION})"
echo "============================================"
echo ""
echo "  Image:   $OUTPUT_IMG"
echo "  Size:    $(du -h "$OUTPUT_IMG" | cut -f1)"
echo ""
echo "  Patches applied:"
echo "    - HDMI output enabled"
echo "    - SPI CAN overlays disabled (no hardware on Pi 5B)"
echo "    - flash-pico.sh (auto-flashes RP2350 Pico on any USB port)"
echo "    - USB-CAN multi-adapter support (can_s0-s4, CAN FD 1Mbps/5Mbps)"
echo "    - vcan placeholders auto-fill missing can_s0-s4 (HAL requires all 5)"
echo "    - CAN is optional (30s timeout, robot starts regardless)"
echo "    - Hot-plug: new adapters auto-named and configured"
echo "    - robot_heartbeat loaded after the buses exist, then mrccomm restarted"
echo "      (creates the real /dev/mrccan/* + the CAN enable heartbeat)"
echo "    - /dev/mrccan tmpfile fallback (unblocks MrcCommDaemon -> robot.service)"
echo "    - i2c-dev loaded at boot"
echo "    - Wireless regulatory database (US WiFi channels, if not already present)"
echo "    - Dashboard: WLAN0 AP settings unlocked, fault count reset button"
echo ""
echo "  Flash to SD card:"
echo "    sudo dd if=$OUTPUT_IMG of=/dev/sdX bs=4M status=progress"
echo ""
echo "  After flashing, just insert SD and power on the Pi 5."
echo "  On first boot, limelight_expandfs grows rootfs A to 7 GiB and creates"
echo "  the (empty) slot B. No further configuration needed."
echo ""

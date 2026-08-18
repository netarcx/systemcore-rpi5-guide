"""Pure-Python MBR partition table parser.

Replaces `sfdisk` — reads the 512-byte MBR and extended partition entries
to produce the same ImageLayout that the Linux patcher derives from sfdisk.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Partition:
    index: int
    start_bytes: int
    size_bytes: int
    fs: str
    present: bool = True  # False when the partition extends past end-of-file

    @property
    def end_bytes(self) -> int:
        return self.start_bytes + self.size_bytes


@dataclass
class ImageLayout:
    image: Path
    partitions: list[Partition]
    boot_a: Optional[Partition] = None
    boot_b: Optional[Partition] = None
    root_a: Optional[Partition] = None
    root_b: Optional[Partition] = None
    # True when the partition table declares a rootfs B that isn't in the
    # file. Beta 14 / Alpha 14 and later ship packed like this; expandfs.sh
    # grows the table and formats slot B on first boot.
    packed: bool = False


MBR_ENTRY_FORMAT = "<B3xB3xII"  # status, type, lba_start, lba_sectors
MBR_ENTRY_SIZE = 16
SECTOR = 512


def _parse_mbr_entries(data: bytes, base_offset: int = 0) -> list[tuple[int, int, int]]:
    """Parse 4 MBR partition table entries. Returns list of (type, start_bytes, size_bytes)."""
    entries = []
    for i in range(4):
        offset = 446 + i * MBR_ENTRY_SIZE
        status, ptype, lba_start, lba_sectors = struct.unpack_from(
            MBR_ENTRY_FORMAT, data, offset
        )
        if ptype == 0 or lba_sectors == 0:
            continue
        start = (base_offset + lba_start) * SECTOR
        size = lba_sectors * SECTOR
        entries.append((ptype, start, size))
    return entries


def _detect_fs_from_bytes(image_path: Path, offset: int, ptype: int) -> str:
    """Identify filesystem by reading magic bytes at the partition start."""
    if ptype in (0x05, 0x0F, 0x85):
        return "extended"

    if offset >= image_path.stat().st_size:
        return "absent"

    with open(image_path, "rb") as f:
        f.seek(offset)
        head = f.read(8192)

    if len(head) >= 0x43A and head[0x438:0x43A] == b"\x53\xef":
        return "ext4"
    if b"FAT32" in head[:90] or b"FAT16" in head[:90] or b"mkfs.fat" in head[:90]:
        return "vfat"
    if len(head) >= 512 and head[510:512] == b"\x55\xaa":
        return "vfat"
    return "unknown"


def detect_layout(image: Path) -> ImageLayout:
    """Parse MBR (and extended partitions) to build the full partition list."""
    image_size = image.stat().st_size
    with open(image, "rb") as f:
        mbr = f.read(512)

    if mbr[510:512] != b"\x55\xaa":
        raise ValueError(f"No MBR signature found in {image}")

    partitions: list[Partition] = []
    entries = _parse_mbr_entries(mbr)

    for ptype, start, size in entries:
        if ptype in (0x05, 0x0F, 0x85):
            ebr_base = start // SECTOR
            ebr_offset = start
            while True:
                with open(image, "rb") as f:
                    f.seek(ebr_offset)
                    ebr = f.read(512)
                if ebr[510:512] != b"\x55\xaa":
                    break
                sub_entries = _parse_mbr_entries(ebr, ebr_offset // SECTOR)
                if not sub_entries:
                    break
                # First entry is the logical partition (relative to this EBR)
                _, logical_start, logical_size = sub_entries[0]
                fs = _detect_fs_from_bytes(image, logical_start, sub_entries[0][0])
                partitions.append(Partition(
                    index=len(partitions) + 1,
                    start_bytes=logical_start,
                    size_bytes=logical_size,
                    fs=fs,
                    present=logical_start + logical_size <= image_size,
                ))
                # Second entry (if present) points to next EBR (relative to extended start)
                if len(sub_entries) < 2:
                    break
                next_type, _, _ = sub_entries[1]
                if next_type not in (0x05, 0x0F, 0x85):
                    break
                # Next EBR offset is relative to the extended partition start
                ebr_offset = (ebr_base + struct.unpack_from("<I", ebr, 446 + MBR_ENTRY_SIZE + 8)[0]) * SECTOR
                if ebr_offset <= start:
                    break
        else:
            fs = _detect_fs_from_bytes(image, start, ptype)
            partitions.append(Partition(
                index=len(partitions) + 1,
                start_bytes=start,
                size_bytes=size,
                fs=fs,
                present=start + size <= image_size,
            ))

    # Identify boot A/B and rootfs A/B by size + type (same heuristic as Linux version)
    fat_parts = sorted(
        [p for p in partitions
         if p.present and p.fs == "vfat" and p.size_bytes < 200 * 1024 * 1024],
        key=lambda p: p.start_bytes,
    )
    ext_parts = sorted(
        [p for p in partitions if p.present and p.fs == "ext4"],
        key=lambda p: p.start_bytes,
    )
    boot_candidates = [p for p in fat_parts if p.size_bytes >= 30 * 1024 * 1024]

    layout = ImageLayout(image=image, partitions=partitions)
    if len(boot_candidates) >= 1:
        layout.boot_a = boot_candidates[0]
    if len(boot_candidates) >= 2:
        layout.boot_b = boot_candidates[1]
    if len(ext_parts) >= 1:
        layout.root_a = ext_parts[0]
    if len(ext_parts) >= 2:
        layout.root_b = ext_parts[1]

    layout.packed = (
        layout.boot_b is not None
        and layout.root_a is not None
        and layout.root_b is None
    )

    return layout

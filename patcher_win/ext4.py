"""ext4 partition access via bundled e2fsprogs debugfs.exe.

Extracts the ext4 partition to a temp file, runs debugfs commands to
read/write files, then splices the modified partition back into the image.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Optional

S_IFREG = 0o100000  # regular-file bits of i_mode

log = logging.getLogger("patcher")

TOOLS_DIR = Path(__file__).resolve().parent / "tools"
DEBUGFS_EXE = TOOLS_DIR / "debugfs.exe"


def _find_debugfs() -> Path:
    """Locate debugfs.exe — bundled in tools/ or on PATH."""
    if DEBUGFS_EXE.exists():
        return DEBUGFS_EXE
    found = shutil.which("debugfs") or shutil.which("debugfs.exe")
    if found:
        return Path(found)
    raise FileNotFoundError(
        f"debugfs.exe not found. Place it in {TOOLS_DIR} or add to PATH.\n"
        "Download e2fsprogs Windows binaries from:\n"
        "  https://github.com/nicuveo/e2fsprogs-win32/releases\n"
        "  or build from https://github.com/tytso/e2fsprogs with MSYS2/MinGW."
    )


class Ext4Partition:
    """Context manager that extracts an ext4 partition from a disk image,
    provides read/write operations via debugfs, then writes it back."""

    def __init__(self, image_path: Path, offset: int, size: int):
        self.image_path = image_path
        self.offset = offset
        self.size = size
        self._tmp_dir: Optional[Path] = None
        self._partition_file: Optional[Path] = None
        self._debugfs = _find_debugfs()
        self._modified = False

    def __enter__(self) -> "Ext4Partition":
        self._tmp_dir = Path(tempfile.mkdtemp(prefix="patcher-ext4-"))
        self._partition_file = self._tmp_dir / "partition.ext4"

        log.info("Extracting ext4 partition (offset=%d, size=%.1f MB) to temp file",
                 self.offset, self.size / (1024 * 1024))

        with open(self.image_path, "rb") as src:
            src.seek(self.offset)
            with open(self._partition_file, "wb") as dst:
                remaining = self.size
                while remaining > 0:
                    chunk = min(remaining, 4 * 1024 * 1024)
                    data = src.read(chunk)
                    if not data:
                        break
                    dst.write(data)
                    remaining -= len(data)

        return self

    def __exit__(self, *exc):
        if self._modified and self._partition_file and self._partition_file.exists():
            log.info("Writing modified ext4 partition back to image")
            with open(self._partition_file, "rb") as src:
                with open(self.image_path, "r+b") as dst:
                    dst.seek(self.offset)
                    remaining = self.size
                    while remaining > 0:
                        chunk = min(remaining, 4 * 1024 * 1024)
                        data = src.read(chunk)
                        if not data:
                            break
                        dst.write(data)
                        remaining -= len(data)

        if self._tmp_dir and self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _run_debugfs(self, commands: list[str], write: bool = False) -> str:
        """Run one or more debugfs commands. Returns combined stdout."""
        cmd_file = self._tmp_dir / "debugfs_cmds.txt"
        cmd_file.write_text("\n".join(commands) + "\n", encoding="utf-8")

        args = [str(self._debugfs)]
        if write:
            args.append("-w")
        args.extend(["-f", str(cmd_file), str(self._partition_file)])

        log.debug("debugfs %s: %s", "(-w)" if write else "(ro)", commands)
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0 and result.stderr.strip():
            # debugfs often prints warnings to stderr that aren't fatal
            for line in result.stderr.strip().splitlines():
                if "error" in line.lower() or "fatal" in line.lower():
                    log.error("debugfs: %s", line)
                else:
                    log.debug("debugfs stderr: %s", line)
        return result.stdout

    def read_file(self, guest_path: str) -> bytes:
        """Read a file from the ext4 partition to a local temp file, return contents."""
        local_out = self._tmp_dir / "read_tmp"
        self._run_debugfs([f"dump {guest_path} {local_out}"])
        if not local_out.exists():
            raise FileNotFoundError(f"debugfs could not read {guest_path}")
        data = local_out.read_bytes()
        local_out.unlink()
        return data

    def read_text(self, guest_path: str) -> str:
        """Read a text file from the ext4 partition."""
        return self.read_file(guest_path).decode("utf-8", errors="surrogateescape")

    def write_file(self, guest_path: str, data: bytes, mode: int = 0o644) -> None:
        """Write data to a file on the ext4 partition."""
        local_tmp = self._tmp_dir / "write_tmp"
        local_tmp.write_bytes(data)

        # Ensure parent directory exists
        parent = str(Path(guest_path).parent).replace("\\", "/")
        if parent and parent != "/":
            self._mkdir_p(parent)

        # Remove existing file first (debugfs 'write' fails if file exists)
        self._run_debugfs([f"rm {guest_path}"], write=True)
        # i_mode holds the file type in its high bits, so the permission bits
        # have to be OR'd with S_IFREG. Setting a bare 0644 leaves a typeless
        # inode: e2fsck reports "invalid mode" / "incorrect filetype" and the
        # kernel refuses to read the directory it lives in.
        self._run_debugfs([
            f"write {local_tmp} {guest_path}",
            f"set_inode_field {guest_path} mode 0{S_IFREG | mode:o}",
        ], write=True)

        local_tmp.unlink()
        self._modified = True
        log.info("  wrote %s (%d bytes, mode %o)", guest_path, len(data), mode)

    def write_text(self, guest_path: str, content: str, mode: int = 0o644) -> None:
        """Write text to a file on the ext4 partition."""
        self.write_file(guest_path, content.encode("utf-8"), mode)

    def copy_file_in(self, local_path: Path, guest_path: str, mode: int = 0o644) -> None:
        """Copy a local file into the ext4 partition."""
        self.write_file(guest_path, local_path.read_bytes(), mode)

    def _mkdir_p(self, guest_path: str) -> None:
        """Create directory and all parents (like mkdir -p)."""
        # PurePosixPath("/etc/foo").parts starts with "/" — passing that root
        # component to debugfs runs `mkdir /`, which adds an entry with an
        # empty name to the root directory ("Entry '' in / has a zero-length
        # name") and makes the mounted filesystem throw EIO on readdir.
        parts = [p for p in PurePosixPath(guest_path).parts if p != "/"]
        for i in range(1, len(parts) + 1):
            partial = "/" + "/".join(parts[:i])
            # `mkdir` on a path that already exists still allocates an inode
            # before failing to link it, leaving an unconnected directory
            # behind (e2fsck: "Unconnected directory inode ... was in ...").
            # Every parent of a file we install already exists, so this guard
            # is the common case, not the exception.
            if self.exists(partial):
                continue
            self._run_debugfs([f"mkdir {partial}"], write=True)
            self._modified = True

    def exists(self, guest_path: str) -> bool:
        """Check if a path exists in the ext4 partition."""
        out = self._run_debugfs([f"stat {guest_path}"])
        return "Type:" in out


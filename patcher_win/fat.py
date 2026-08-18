"""FAT32 partition access using pyfatfs.

Provides file read/write/copy operations on a FAT32 partition embedded
inside a raw disk image at a known byte offset. No mounting required.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fs.base import FS

log = logging.getLogger("patcher")


class _KeepOpenBytesIO(io.BytesIO):
    """BytesIO whose close() is a no-op.

    pyfatfs closes the file object it is handed, and closing a BytesIO throws
    its buffer away — so the patched partition bytes would be gone before they
    could be written back to the image (the read-back raised "I/O operation on
    closed file" instead). Ignoring close() keeps the buffer alive; the flush
    that close() performs still lands in it. Call `release()` once the data is
    safely out.
    """

    def close(self) -> None:  # noqa: D102 - deliberately does nothing
        pass

    def release(self) -> None:
        super().close()


class FatPartition:
    """Context manager wrapping a pyfatfs filesystem at a given image offset."""

    def __init__(self, image_path: Path, offset: int, size: int):
        self.image_path = image_path
        self.offset = offset
        self.size = size
        self._fs: Any = None
        self._stream: _KeepOpenBytesIO | None = None

    def __enter__(self) -> "FatPartition":
        from pyfatfs.PyFatFS import PyFatBytesIOFS

        with open(self.image_path, "rb") as f:
            f.seek(self.offset)
            partition_data = f.read(self.size)

        self._stream = _KeepOpenBytesIO(partition_data)
        self._fs = PyFatBytesIOFS(fp=self._stream, encoding="utf-8")
        return self

    def __exit__(self, exc_type, exc, tb):
        stream, self._stream = self._stream, None

        if self._fs is not None:
            try:
                self._fs.close()
            except Exception:
                # Closing flushes pyfatfs's buffers. If the body already
                # raised, that error is the interesting one — don't let a
                # secondary close failure replace it.
                if exc_type is None:
                    raise
                log.debug("Ignoring FAT close error during unwind", exc_info=True)
            finally:
                self._fs = None

        # Never flush a half-patched partition back into the image: on failure
        # the caller discards the working file, and attempting the writeback
        # here would also mask the original exception.
        if stream is None or exc_type is not None:
            return

        stream.seek(0)
        modified_data = stream.read()
        stream.release()

        if len(modified_data) != self.size:
            raise RuntimeError(
                f"FAT partition at offset {self.offset} changed size "
                f"({len(modified_data)} != {self.size}); refusing to write back"
            )

        with open(self.image_path, "r+b") as f:
            f.seek(self.offset)
            f.write(modified_data)

    @property
    def fs(self) -> Any:
        if self._fs is None:
            raise RuntimeError("FatPartition not opened — use as context manager")
        return self._fs

    def _resolve(self, path: str) -> str:
        """Resolve `path` against the on-disk entries, ignoring case.

        FAT is a case-insensitive filesystem, and pyfatfs reports entries that
        fit the old 8.3 form in upper case — the boot partitions really do list
        "CONFIG.TXT" and "CMDLINE.TXT" — so a literal lookup of "/config.txt"
        misses a file that is right there. Names too long or too mixed for 8.3
        (e.g. "bcm2712-rpi-5-b.dtb") come back verbatim, so both must work.

        A component with no match keeps the spelling the caller asked for, so
        this is also safe for paths being created.
        """
        resolved = ""
        for part in PurePosixPath(path).parts:
            if part in ("/", ""):
                continue
            candidate = f"{resolved}/{part}"
            if self.fs.exists(candidate):
                resolved = candidate
                continue
            try:
                entries = self.fs.listdir(resolved or "/")
            except Exception:
                return path
            match = next((e for e in entries if e.lower() == part.lower()), None)
            resolved = f"{resolved}/{match}" if match else candidate
        return resolved or "/"

    def read_text(self, path: str) -> str:
        """Read a text file from the FAT partition."""
        with self.fs.open(self._resolve(path), "r") as f:
            return f.read()

    def write_text(self, path: str, content: str) -> None:
        """Write a text file to the FAT partition, creating parent dirs."""
        path = self._prepare_write(path)
        with self.fs.open(path, "w") as f:
            f.write(content)
        log.info("  wrote %s (%d bytes)", path, len(content))

    def write_binary(self, path: str, data: bytes) -> None:
        """Write binary data to a file on the FAT partition."""
        path = self._prepare_write(path)
        with self.fs.open(path, "wb") as f:
            f.write(data)
        log.info("  wrote %s (%d bytes)", path, len(data))

    def _prepare_write(self, path: str) -> str:
        """Resolve `path`, make its parent, and clear any existing file.

        Overwriting in place is not an option: pyfatfs truncates the target
        when a file is opened for writing, which frees its cluster chain and
        then trips over the freed clusters on the first write ("FREE_CLUSTER
        mark found in FAT cluster chain"). Removing the entry first and
        creating it fresh side-steps that.
        """
        path = self._resolve(path)
        parent = str(PurePosixPath(path).parent)
        if parent != "/" and not self.fs.isdir(parent):
            self.fs.makedirs(parent, recreate=True)
        if self.fs.exists(path) and not self.fs.isdir(path):
            self.fs.remove(path)
        return path

    def copy_file_in(self, local_path: Path, dest_path: str) -> None:
        """Copy a local file into the FAT partition."""
        data = local_path.read_bytes()
        self.write_binary(dest_path, data)

    def exists(self, path: str) -> bool:
        """Check if a file or directory exists (case-insensitively, as FAT does)."""
        return self.fs.exists(self._resolve(path))


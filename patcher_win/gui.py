"""Windows-native Tkinter GUI for the SystemCore Pi 5B image patcher.

Same layout and UX as patcher/gui.py but runs on Windows Python without
WSL, root, or X11. Uses pyfatfs + debugfs.exe for image manipulation.
"""

from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import core


# ---------------------------------------------------------------------------
# Logging plumbing
# ---------------------------------------------------------------------------


class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:
            self.handleError(record)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class PatcherGUI:
    BOOT_PATCHES = [
        ("enable_hdmi", "Enable HDMI"),
        ("disable_spi_can", "Disable SPI CAN overlays"),
        ("update_cmdline", "Add panic=0 + US wifi regdom"),
    ]
    ROOTFS_PATCHES = [
        ("install_flash_pico", "Install flash-pico.sh"),
        ("install_can_udev", "USB-CAN udev rule"),
        ("install_canbusprocess", "canbusprocess override (vcan placeholders)"),
        ("install_canbuswatchdog", "canbuswatchdog override"),
        ("install_robot_override", "robot.service override"),
        ("install_mrccan", "/dev/mrccan tmpfile (MrcCommDaemon fallback)"),
        ("install_modules_load", "Load i2c-dev at boot"),
        ("install_camera_shim", "Camera on a bare Pi 5B USB port (realpath shim)"),
        ("install_regdb", "Wireless regulatory database"),
        ("patch_dashboard_wlan", "Dashboard: unlock WLAN0 AP"),
        ("patch_dashboard_faults", "Dashboard: fault count reset button"),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SystemCore Pi 5B Image Patcher (Windows)")
        self.root.geometry("980x920")
        self.root.minsize(820, 750)

        self.opts = core.PatchOptions()
        self.toggle_vars: dict[str, tk.BooleanVar] = {}
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None

        self._build_layout()
        self._attach_logging()
        self._drain_log_queue()
        self._check_deps()

    def _check_deps(self) -> None:
        """Log dependency status at startup."""
        log = logging.getLogger("patcher")
        try:
            import pyfatfs  # noqa: F401
            log.info("pyfatfs: OK")
        except ImportError:
            log.error("pyfatfs NOT FOUND — run: pip install pyfatfs")

        try:
            import zstandard  # noqa: F401
            log.info("zstandard: OK")
        except ImportError:
            log.warning("zstandard not installed (needed for regdb .deb) — pip install zstandard")

        try:
            from .ext4 import _find_debugfs
            path = _find_debugfs()
            log.info("debugfs: OK (%s)", path)
        except FileNotFoundError as e:
            log.error("debugfs NOT FOUND — %s", e)

    # -- layout -------------------------------------------------------------

    def _build_layout(self) -> None:
        # Image file pickers
        top = ttk.LabelFrame(self.root, text="Image files", padding=8)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self._file_row(top, 0, "Input image:", self.input_var, self._pick_input)
        self._file_row(top, 1, "Output image:", self.output_var, self._pick_output)

        # Source paths
        self.flash_pico_var = tk.StringVar(value=str(core.DEFAULT_FLASH_PICO))
        self.regdb_var = tk.StringVar(value=str(core.DEFAULT_REGDB_DEB))

        paths = ttk.LabelFrame(self.root, text="Source paths", padding=8)
        paths.pack(fill="x", padx=10, pady=4)
        self._file_row(paths, 0, "flash-pico.sh:", self.flash_pico_var,
                       self._pick_flash_pico)
        self._file_row(paths, 1, "wireless-regdb .deb:", self.regdb_var,
                       self._pick_regdb)

        # Patch toggles
        patches = ttk.Frame(self.root, padding=(10, 4))
        patches.pack(fill="x")
        patches.columnconfigure(0, weight=1)
        patches.columnconfigure(1, weight=1)
        self._patch_column(patches, 0, "Boot partitions (A + B)", self.BOOT_PATCHES)
        self._patch_column(patches, 1, "Rootfs (every slot present)", self.ROOTFS_PATCHES)

        # Advanced options
        adv = ttk.LabelFrame(self.root, text="Options", padding=8)
        adv.pack(fill="x", padx=10, pady=4)

        self.dry_run_var = tk.BooleanVar()
        self.verbose_var = tk.BooleanVar(value=True)
        self.backup_var = tk.BooleanVar()
        self.skip_b_var = tk.BooleanVar()
        self.validate_var = tk.BooleanVar()

        for col, (label, var, tip) in enumerate([
            ("Dry run (log only)", self.dry_run_var, "Don't modify anything"),
            ("Verbose log", self.verbose_var, "Show detailed output"),
            ("Backup input image", self.backup_var, "Copy input to .bak first"),
            ("Patch A only (skip B)", self.skip_b_var,
             "Only patch the A partitions"),
            ("Validate after patch", self.validate_var,
             "Verify expected files exist after patching"),
        ]):
            cb = ttk.Checkbutton(adv, text=label, variable=var)
            cb.grid(row=col // 3, column=col % 3, sticky="w", padx=8, pady=2)
            self._tip(cb, tip)

        # Action buttons
        btns = ttk.Frame(self.root, padding=(10, 4))
        btns.pack(fill="x")
        self.patch_btn = ttk.Button(btns, text="Patch Image", command=self._on_patch)
        self.patch_btn.pack(side="left")
        ttk.Button(btns, text="Show Partitions", command=self._on_show_parts).pack(
            side="left", padx=(8, 0))
        ttk.Button(btns, text="Validate", command=self._on_validate).pack(
            side="left", padx=(8, 0))
        ttk.Button(btns, text="Quit", command=self.root.destroy).pack(side="right")

        # Progress
        prog = ttk.Frame(self.root, padding=(10, 0))
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(prog, textvariable=self.status_var, width=24, anchor="e").pack(
            side="right", padx=(8, 0))

        # Log viewer
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self.log_widget = scrolledtext.ScrolledText(
            log_frame, height=20, wrap="word", font=("Consolas", 9))
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.tag_config("ERROR", foreground="#cc2222")
        self.log_widget.tag_config("WARNING", foreground="#cc8800")
        self.log_widget.tag_config("DEBUG", foreground="#888888")

        log_btns = ttk.Frame(log_frame)
        log_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(log_btns, text="Clear", command=self._clear_log).pack(side="left")
        ttk.Button(log_btns, text="Save log...", command=self._save_log).pack(
            side="left", padx=(4, 0))

    def _file_row(self, parent, row: int, label: str, var: tk.StringVar,
                  cmd, is_dir: bool = False) -> None:
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w")
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", padx=4)
        parent.columnconfigure(1, weight=1)
        ttk.Button(parent, text="Browse...", command=cmd).grid(row=row, column=2)

    def _patch_column(self, parent, col: int, title: str,
                      patches: list[tuple[str, str]]) -> None:
        frame = ttk.LabelFrame(parent, text=title, padding=6)
        frame.grid(row=0, column=col, sticky="nsew", padx=4)
        for i, (name, label) in enumerate(patches):
            var = tk.BooleanVar(value=getattr(self.opts, name))
            self.toggle_vars[name] = var
            cb = ttk.Checkbutton(frame, text=label, variable=var)
            cb.grid(row=i, column=0, sticky="w")
            tip = core.PATCH_DESCRIPTIONS.get(name, "")
            if tip:
                self._tip(cb, tip)

    def _tip(self, widget, text: str) -> None:
        tip_window: dict[str, tk.Toplevel | None] = {"w": None}

        def show(_event=None):
            if tip_window["w"] is not None:
                return
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            tk.Label(tw, text=text, justify="left", background="#ffffe0",
                     relief="solid", borderwidth=1, font=("TkDefaultFont", 9),
                     wraplength=420, padx=4, pady=2).pack()
            tip_window["w"] = tw

        def hide(_event=None):
            if tip_window["w"] is not None:
                tip_window["w"].destroy()
                tip_window["w"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    # -- file pickers -------------------------------------------------------

    def _initial_dir(self) -> str:
        """Return a sensible starting directory for file dialogs."""
        # If input is already set, start in its directory
        if self.input_var.get():
            p = Path(self.input_var.get()).parent
            if p.exists():
                return str(p)
        # Fall back to the script's directory (project root)
        return str(core.PROJECT_ROOT)

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select upstream SystemCore image",
            initialdir=self._initial_dir(),
            filetypes=[("Disk images", "*.img *.zip *.xz"), ("All files", "*.*")])
        if path:
            self.input_var.set(path)
            if not self.output_var.get():
                p = Path(path)
                stem = p.stem
                if stem.endswith(".img"):
                    stem = stem[:-4]
                self.output_var.set(str(p.with_name(stem + "-pi5b.img")))

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Output image path",
            initialdir=self._initial_dir(),
            defaultextension=".img",
            filetypes=[("Disk images", "*.img"), ("All files", "*.*")])
        if path:
            self.output_var.set(path)

    def _pick_flash_pico(self) -> None:
        path = filedialog.askopenfilename(
            title="flash-pico.sh",
            initialdir=self._initial_dir())
        if path:
            self.flash_pico_var.set(path)

    def _pick_regdb(self) -> None:
        path = filedialog.askopenfilename(
            title="wireless-regdb .deb",
            initialdir=self._initial_dir(),
            filetypes=[("Debian packages", "*.deb"), ("All files", "*.*")])
        if path:
            self.regdb_var.set(path)

    # -- option assembly ----------------------------------------------------

    def _collect_opts(self) -> core.PatchOptions | None:
        if not self.input_var.get():
            messagebox.showerror("Missing input", "Pick an input image first.")
            return None
        if not self.output_var.get():
            messagebox.showerror("Missing output", "Pick an output image path.")
            return None

        opts = core.PatchOptions(
            input_image=Path(self.input_var.get()),
            output_image=Path(self.output_var.get()),
            flash_pico_path=Path(self.flash_pico_var.get()),
            regdb_deb_path=Path(self.regdb_var.get()),
            dry_run=self.dry_run_var.get(),
            verbose=self.verbose_var.get(),
            backup=self.backup_var.get(),
            skip_b_partitions=self.skip_b_var.get(),
            validate_after=self.validate_var.get(),
        )
        for name, var in self.toggle_vars.items():
            setattr(opts, name, var.get())
        return opts

    # -- actions ------------------------------------------------------------

    def _on_patch(self) -> None:
        opts = self._collect_opts()
        if not opts:
            return
        self._run_in_worker(
            "Patching",
            lambda: core.patch_image(opts, logging.getLogger("patcher"),
                                     progress=self._on_progress),
        )

    def _on_show_parts(self) -> None:
        path = self.input_var.get() or self.output_var.get()
        if not path:
            messagebox.showerror("No image", "Pick an image first.")
            return

        def go():
            from .partition import detect_layout
            log = logging.getLogger("patcher")
            layout = detect_layout(Path(path))
            for label, part in [("boot_a", layout.boot_a), ("boot_b", layout.boot_b),
                                ("root_a", layout.root_a), ("root_b", layout.root_b)]:
                if part:
                    log.info("  %s: p%d %s offset=%d size=%.1f MB",
                             label, part.index, part.fs, part.start_bytes,
                             part.size_bytes / (1024 * 1024))
                else:
                    log.warning("  %s: NOT FOUND", label)

        self._run_in_worker("Reading partitions", go)

    def _on_validate(self) -> None:
        path = self.output_var.get() or self.input_var.get()
        if not path:
            messagebox.showerror("No image", "Pick an image to validate.")
            return

        def go():
            from .partition import detect_layout
            layout = detect_layout(Path(path))
            problems = core.validate(layout, logging.getLogger("patcher"))
            if not problems:
                self.root.after(0, lambda: messagebox.showinfo(
                    "Validation", "Image looks good."))
            else:
                self.root.after(0, lambda: messagebox.showerror(
                    "Validation problems", "\n".join(problems)))

        self._run_in_worker("Validating", go)

    # -- worker thread plumbing --------------------------------------------

    def _run_in_worker(self, status: str, target) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Another operation is running.")
            return
        self.status_var.set(status + "...")
        self.progress["value"] = 0
        self.patch_btn["state"] = "disabled"

        def runner():
            try:
                target()
                self.root.after(0, lambda: self.status_var.set("Done"))
            except Exception as e:
                logging.getLogger("patcher").error("Failed: %s", e)
                logging.getLogger("patcher").debug(traceback.format_exc())
                self.root.after(0, lambda: self.status_var.set("Failed"))
                self.root.after(0, lambda: messagebox.showerror("Failed", str(e)))
            finally:
                self.root.after(0, lambda: self.patch_btn.configure(state="normal"))

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def _on_progress(self, phase: str, step: int, total: int) -> None:
        pct = int(100 * step / max(total, 1))
        self.root.after(0, lambda: self.progress.configure(value=pct))
        self.root.after(0, lambda: self.status_var.set(f"{phase} ({step}/{total})"))

    # -- log plumbing -------------------------------------------------------

    def _attach_logging(self) -> None:
        root_logger = logging.getLogger("patcher")
        root_logger.setLevel(logging.DEBUG)
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        root_logger.addHandler(handler)

    def _drain_log_queue(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                tag = "INFO"
                for level in ("ERROR", "WARNING", "DEBUG"):
                    if f"[{level}]" in msg:
                        tag = level
                        break
                self.log_widget.insert("end", msg + "\n", tag)
                self.log_widget.see("end")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log_queue)

    def _clear_log(self) -> None:
        self.log_widget.delete("1.0", "end")

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save log to", defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text", "*.txt"), ("All", "*.*")])
        if path:
            Path(path).write_text(self.log_widget.get("1.0", "end"))


def run() -> None:
    """Launch the GUI."""
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass
    PatcherGUI(root)
    root.mainloop()

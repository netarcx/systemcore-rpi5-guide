"""Tkinter GUI for the SystemCore Pi 5B image patcher.

The GUI is a thin wrapper over `patcher.core` — it collects options into a
`PatchOptions`, spawns a worker thread, and streams log records into a
scrollable text widget. Long-running work never blocks the UI thread.
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
# Logging plumbing — pipe records from the worker thread to the GUI
# ---------------------------------------------------------------------------


class QueueHandler(logging.Handler):
    """Push log records onto a queue so the GUI thread can drain them."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.q.put(self.format(record))
        except Exception:  # noqa: BLE001 — last resort
            self.handleError(record)


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class PatcherGUI:
    """Tk root + all widgets. Held by `run()` for its lifetime."""

    BOOT_PATCHES = [
        ("enable_hdmi", "Enable HDMI"),
        ("disable_spi_can", "Disable SPI CAN overlays"),
        ("update_cmdline", "Add panic=0 + US wifi regdom"),
    ]
    ALPHA_BOOT_PATCHES = [
        ("fix_autoboot", "Disable autoboot.txt redirect"),
        ("fix_config_sections", "Fix config.txt [all] section"),
        ("fix_cmdline_reference", "Create cmdline.txt"),
        ("install_pi5b_dtb", "Install Pi 5B device tree"),
        ("comment_pi4_firmware", "Comment out Pi 4 firmware"),
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
        ("disable_iodaemon", "Keep iodaemon off (Pi 5B brownout blocks CAN enable)"),
        ("install_regdb", "Wireless regulatory database"),
        ("patch_dashboard_wlan", "Dashboard: unlock WLAN0 AP"),
        ("patch_dashboard_faults", "Dashboard: fault count reset button"),
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("SystemCore Pi 5B Image Patcher")
        self.root.geometry("1080x980")
        self.root.minsize(1020, 820)

        self.opts = core.PatchOptions()
        self.toggle_vars: dict[str, tk.BooleanVar] = {}
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.cancel_requested = threading.Event()

        self._build_layout()
        self._attach_logging()
        self._drain_log_queue()

    # -- layout -------------------------------------------------------------

    def _build_layout(self) -> None:
        # Top: file pickers
        top = ttk.LabelFrame(self.root, text="Image files", padding=8)
        top.pack(fill="x", padx=10, pady=(10, 4))

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()

        self._file_row(top, 0, "Input image:", self.input_var, self._pick_input)
        self._file_row(top, 1, "Output image:", self.output_var, self._pick_output)

        # Path overrides for flash-pico / regdb
        self.flash_pico_var = tk.StringVar(value=str(core.DEFAULT_FLASH_PICO))
        self.regdb_var = tk.StringVar(value=str(core.DEFAULT_REGDB_DEB))

        paths = ttk.LabelFrame(self.root, text="Source paths (auto-filled from repo)", padding=8)
        paths.pack(fill="x", padx=10, pady=4)
        self._file_row(paths, 0, "flash-pico.sh:", self.flash_pico_var, self._pick_flash_pico)
        self._file_row(paths, 1, "wireless-regdb .deb:", self.regdb_var, self._pick_regdb)

        # Middle: two side-by-side columns of patch checkboxes
        patches = ttk.Frame(self.root, padding=(10, 4))
        patches.pack(fill="x")
        patches.columnconfigure(0, weight=1)
        patches.columnconfigure(1, weight=1)
        patches.columnconfigure(2, weight=1)

        self._patch_column(patches, 0, "Boot (shared)", self.BOOT_PATCHES)
        self._patch_column(patches, 1, "Boot (Alpha 2-10 only)", self.ALPHA_BOOT_PATCHES)
        self._patch_column(patches, 2, "Rootfs (every slot present)", self.ROOTFS_PATCHES)

        # Advanced / debugging options
        adv = ttk.LabelFrame(self.root, text="Debug + advanced", padding=8)
        adv.pack(fill="x", padx=10, pady=4)

        self.dry_run_var = tk.BooleanVar()
        self.verbose_var = tk.BooleanVar(value=True)
        self.backup_var = tk.BooleanVar()
        self.keep_mounted_var = tk.BooleanVar()
        self.no_cleanup_var = tk.BooleanVar()
        self.skip_b_var = tk.BooleanVar()
        self.validate_var = tk.BooleanVar()

        for col, (label, var, tip) in enumerate([
            ("Dry run (log only)", self.dry_run_var, "Don't modify anything"),
            ("Verbose log", self.verbose_var, "Show every shell command"),
            ("Backup input image", self.backup_var, "Copy input to .bak before patching"),
            ("Keep mounted after", self.keep_mounted_var,
             "Don't unmount partitions on success (for inspection)"),
            ("No cleanup on error", self.no_cleanup_var,
             "Leave mounts open if a patch fails (for debugging)"),
            ("Patch A only (skip B)", self.skip_b_var,
             "Useful when only one half is interesting"),
            ("Validate after patch", self.validate_var,
             "Re-mount and verify expected files exist"),
        ]):
            cb = ttk.Checkbutton(adv, text=label, variable=var)
            cb.grid(row=col // 4, column=col % 4, sticky="w", padx=4, pady=2)
            self._tip(cb, tip)

        # Action buttons
        btns = ttk.Frame(self.root, padding=(10, 4))
        btns.pack(fill="x")
        self.patch_btn = ttk.Button(btns, text="Patch image", command=self._on_patch)
        self.patch_btn.pack(side="left")
        ttk.Button(btns, text="Inspect (mount only)", command=self._on_inspect).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btns, text="Validate", command=self._on_validate).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(btns, text="Show partitions", command=self._on_show_parts).pack(
            side="left", padx=(8, 0)
        )
        self.cancel_btn = ttk.Button(btns, text="Cancel", state="disabled",
                                      command=self._on_cancel)
        self.cancel_btn.pack(side="left", padx=(8, 0))
        ttk.Button(btns, text="Quit", command=self.root.destroy).pack(side="right")

        # Progress + status
        prog = ttk.Frame(self.root, padding=(10, 0))
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(prog, textvariable=self.status_var, width=24, anchor="e").pack(
            side="right", padx=(8, 0)
        )

        # Log viewer
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))

        self.log_widget = scrolledtext.ScrolledText(
            log_frame, height=28, wrap="word", font=("Courier New", 9)
        )
        self.log_widget.pack(fill="both", expand=True)
        self.log_widget.tag_config("ERROR", foreground="#cc2222")
        self.log_widget.tag_config("WARNING", foreground="#cc8800")
        self.log_widget.tag_config("DEBUG", foreground="#888888")

        log_btns = ttk.Frame(log_frame)
        log_btns.pack(fill="x", pady=(4, 0))
        ttk.Button(log_btns, text="Clear", command=self._clear_log).pack(side="left")
        ttk.Button(log_btns, text="Save log...", command=self._save_log).pack(
            side="left", padx=(4, 0)
        )

    def _file_row(self, parent: ttk.LabelFrame, row: int, label: str,
                  var: tk.StringVar, cmd, is_dir: bool = False) -> None:
        ttk.Label(parent, text=label, width=20).grid(row=row, column=0, sticky="w")
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", padx=4)
        parent.columnconfigure(1, weight=1)
        ttk.Button(parent, text="Browse...", command=cmd).grid(row=row, column=2)

    def _patch_column(self, parent: ttk.Frame, col: int, title: str,
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
        """Tooltip that shows on hover — keeps the UI uncluttered without
        forcing a dedicated help column for every checkbox."""
        tip_window: dict[str, tk.Toplevel | None] = {"w": None}

        def show(_event=None):
            if tip_window["w"] is not None:
                return
            x = widget.winfo_rootx() + 20
            y = widget.winfo_rooty() + widget.winfo_height() + 2
            tw = tk.Toplevel(widget)
            tw.wm_overrideredirect(True)
            tw.wm_geometry(f"+{x}+{y}")
            tk.Label(
                tw, text=text, justify="left",
                background="#ffffe0", relief="solid", borderwidth=1,
                font=("TkDefaultFont", 9), wraplength=420, padx=4, pady=2,
            ).pack()
            tip_window["w"] = tw

        def hide(_event=None):
            if tip_window["w"] is not None:
                tip_window["w"].destroy()
                tip_window["w"] = None

        widget.bind("<Enter>", show)
        widget.bind("<Leave>", hide)

    # -- file pickers -------------------------------------------------------

    def _pick_input(self) -> None:
        path = filedialog.askopenfilename(
            title="Select upstream SystemCore image",
            filetypes=[("Disk images", "*.img *.zip"), ("All files", "*.*")],
        )
        if path:
            self.input_var.set(path)
            if not self.output_var.get():
                p = Path(path)
                self.output_var.set(str(p.with_name(p.stem + "-pi5b" + p.suffix)))

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Output image path",
            defaultextension=".img",
            filetypes=[("Disk images", "*.img"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _pick_flash_pico(self) -> None:
        path = filedialog.askopenfilename(title="flash-pico.sh")
        if path:
            self.flash_pico_var.set(path)

    def _pick_regdb(self) -> None:
        path = filedialog.askopenfilename(
            title="wireless-regdb .deb",
            filetypes=[("Debian packages", "*.deb"), ("All files", "*.*")],
        )
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
            keep_mounted=self.keep_mounted_var.get(),
            cleanup_on_error=not self.no_cleanup_var.get(),
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

    def _on_inspect(self) -> None:
        path = self.input_var.get() or self.output_var.get()
        if not path:
            messagebox.showerror("No image", "Pick an image first.")
            return

        def go():
            from .core import detect_layout, inspect
            log = logging.getLogger("patcher")
            done = threading.Event()
            layout = detect_layout(Path(path), log)
            mounts = inspect(layout, log)
            tracker = mounts.pop("_tracker")
            paths = "\n".join(f"  {k}: {v}" for k, v in mounts.items())

            def show_dialog_then_cleanup():
                # Runs on the GUI thread: modal messagebox blocks until
                # the user clicks OK, then we unmount and signal the worker.
                messagebox.showinfo(
                    "Mounted",
                    f"Partitions are mounted at:\n\n{paths}\n\n"
                    f"Inspect them with a file manager or shell. Click OK when "
                    f"done to unmount.",
                )
                tracker.cleanup(log)
                done.set()

            self.root.after(0, show_dialog_then_cleanup)
            done.wait()

        self._run_in_worker("Inspecting", go)

    def _on_validate(self) -> None:
        path = self.output_var.get() or self.input_var.get()
        if not path:
            messagebox.showerror("No image", "Pick an image to validate.")
            return

        def go():
            from .core import detect_layout, validate
            layout = detect_layout(Path(path), logging.getLogger("patcher"))
            problems = validate(layout, logging.getLogger("patcher"))
            if not problems:
                self.root.after(0, lambda: messagebox.showinfo(
                    "Validation",
                    f"Image looks good: all expected files present in rootfs A/B."
                ))
            else:
                self.root.after(0, lambda: messagebox.showerror(
                    "Validation problems",
                    "\n".join(problems),
                ))

        self._run_in_worker("Validating", go)

    def _on_show_parts(self) -> None:
        path = self.input_var.get() or self.output_var.get()
        if not path:
            messagebox.showerror("No image", "Pick an image first.")
            return

        def go():
            from .core import detect_layout
            detect_layout(Path(path), logging.getLogger("patcher"))

        self._run_in_worker("Reading partitions", go)

    def _on_cancel(self) -> None:
        self.cancel_requested.set()
        self.status_var.set("Cancel requested...")
        # Real cancellation requires the worker to cooperate. The current
        # `core.patch_image` runs blocking subprocess.run calls and won't
        # check the flag — but setting it gives the user feedback and the
        # GUI a chance to refuse new work until the worker exits.

    # -- worker thread plumbing --------------------------------------------

    def _run_in_worker(self, status: str, target) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("Busy", "Another operation is already running.")
            return
        self.cancel_requested.clear()
        self.status_var.set(status + "...")
        self.progress["value"] = 0
        self.patch_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"

        def runner():
            try:
                target()
                self.root.after(0, lambda: self.status_var.set("Done"))
            except Exception as e:  # noqa: BLE001 — surface anything to user
                logging.getLogger("patcher").error("Failed: %s", e)
                logging.getLogger("patcher").debug(traceback.format_exc())
                self.root.after(0, lambda: self.status_var.set("Failed"))
                self.root.after(0, lambda: messagebox.showerror("Failed", str(e)))
            finally:
                self.root.after(0, self._reset_buttons)

        self.worker = threading.Thread(target=runner, daemon=True)
        self.worker.start()

    def _reset_buttons(self) -> None:
        self.patch_btn["state"] = "normal"
        self.cancel_btn["state"] = "disabled"

    def _on_progress(self, phase: str, step: int, total: int) -> None:
        pct = int(100 * step / max(total, 1))
        # Cross thread: schedule on UI thread.
        self.root.after(0, lambda: self.progress.configure(value=pct))
        self.root.after(0, lambda: self.status_var.set(f"{phase} ({step}/{total})"))

    # -- log plumbing -------------------------------------------------------

    def _attach_logging(self) -> None:
        root_logger = logging.getLogger("patcher")
        root_logger.setLevel(logging.DEBUG)
        # Strip any existing handlers from prior runs (REPL or repeated init).
        for h in list(root_logger.handlers):
            root_logger.removeHandler(h)
        handler = QueueHandler(self.log_queue)
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                              datefmt="%H:%M:%S")
        )
        root_logger.addHandler(handler)
        # Stream to stdout too — useful when launching with `python -m patcher`.
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
        root_logger.addHandler(stream)

    def _drain_log_queue(self) -> None:
        """Pull log records from the queue and append to the text widget."""
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
            title="Save log to",
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"),
                       ("All files", "*.*")],
        )
        if path:
            Path(path).write_text(self.log_widget.get("1.0", "end"))


def run() -> None:
    """Launch the GUI event loop."""
    root = tk.Tk()
    try:
        style = ttk.Style()
        # 'clam' is the most consistent ttk theme across platforms.
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass
    PatcherGUI(root)
    root.mainloop()

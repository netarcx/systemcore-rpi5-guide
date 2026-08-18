"""Command-line interface for the SystemCore Pi 5B image patcher.

Designed so the same options available in the GUI are reachable headlessly
(useful from CI, build scripts, or remote sessions where Tk isn't installed).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import core


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="patch-image",
        description=(
            "Patch an upstream SystemCore image with Pi 5B-compatible "
            "modifications. Run with no arguments to launch the GUI."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  sudo patch-image                                  # launch GUI\n"
            "  sudo patch-image upstream.img                     # patch in place\n"
            "  sudo patch-image upstream.img -o patched.img      # patch to new file\n"
            "  sudo patch-image upstream.img --only install_mrccan,install_can_udev\n"
            "  sudo patch-image upstream.img --dry-run --verbose # show plan, do nothing\n"
            "  sudo patch-image upstream.img --inspect           # mount + leave open\n"
        ),
    )

    p.add_argument("input", nargs="?",
                   help="Input image file. Omit to launch GUI.")
    p.add_argument("-o", "--output", help="Output image file (default: alongside input).")

    # Source paths
    p.add_argument("--flash-pico", default=str(core.DEFAULT_FLASH_PICO),
                   help="Path to flash-pico.sh (default: %(default)s).")
    p.add_argument("--regdb", default=str(core.DEFAULT_REGDB_DEB),
                   help="Path to wireless-regdb .deb (default: %(default)s).")

    # Operational modes
    p.add_argument("--gui", action="store_true",
                   help="Force GUI even if positional args are given.")
    p.add_argument("--dry-run", action="store_true",
                   help="Log what would happen but don't modify anything.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Verbose logging — show every shell command.")
    p.add_argument("--backup", action="store_true",
                   help="Copy input to .bak before in-place patching.")
    p.add_argument("--keep-mounted", action="store_true",
                   help="Don't unmount partitions on success.")
    p.add_argument("--no-cleanup-on-error", action="store_true",
                   help="Leave mounts open if a patch fails.")
    p.add_argument("--skip-b", action="store_true",
                   help="Only patch A partitions, skip B.")
    p.add_argument("--validate", action="store_true",
                   help="Re-mount the output image and verify expected files.")

    p.add_argument("--image-type", choices=["auto", "alpha", "ab", "beta", "beta10"],
                   default="auto",
                   help="Force image type: 'alpha' for the Alpha 2-10 single-boot "
                        "layout, 'ab' for the Beta 10+ / Alpha 11+ A/B layout "
                        "('beta'/'beta10' are accepted spellings of 'ab'). "
                        "Default: auto-detect from partition layout.")

    # Diagnostic-only modes
    p.add_argument("--inspect", action="store_true",
                   help="Mount partitions, print paths, and wait for ENTER.")
    p.add_argument("--show-partitions", action="store_true",
                   help="Print partition layout and exit.")
    p.add_argument("--list-patches", action="store_true",
                   help="List available patches with descriptions and exit.")

    # Patch selection
    p.add_argument("--only",
                   help="Comma-separated list of patches to apply (everything else skipped).")
    for name in core.PatchOptions().patch_names():
        flag = "--no-" + name.replace("_", "-")
        p.add_argument(flag, dest=f"no_{name}", action="store_true",
                       help=f"Skip: {core.PATCH_DESCRIPTIONS.get(name, name)}")

    return p


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def setup_logging(verbose: bool) -> logging.Logger:
    log = logging.getLogger("patcher")
    log.handlers.clear()
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    log.addHandler(handler)
    return log


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def options_from_args(args: argparse.Namespace) -> core.PatchOptions:
    opts = core.PatchOptions()

    if args.input:
        opts.input_image = Path(args.input)
    if args.output:
        opts.output_image = Path(args.output)
    elif args.input:
        # Default: <input>-pi5b<ext>.
        inp = Path(args.input)
        opts.output_image = inp.with_name(inp.stem + "-pi5b" + inp.suffix)

    opts.flash_pico_path = Path(args.flash_pico)
    opts.regdb_deb_path = Path(args.regdb)

    opts.force_image_type = core.IMAGE_TYPE_ALIASES.get(args.image_type)
    opts.dry_run = args.dry_run
    opts.verbose = args.verbose
    opts.backup = args.backup
    opts.keep_mounted = args.keep_mounted
    opts.cleanup_on_error = not args.no_cleanup_on_error
    opts.skip_b_partitions = args.skip_b
    opts.validate_after = args.validate

    # --only takes precedence over individual --no-* flags: it sets *every*
    # toggle off except the named ones.
    if args.only:
        wanted = {name.strip() for name in args.only.split(",")}
        unknown = wanted - set(opts.patch_names())
        if unknown:
            raise SystemExit(f"Unknown patch name(s): {', '.join(sorted(unknown))}")
        for name in opts.patch_names():
            setattr(opts, name, name in wanted)
    else:
        for name in opts.patch_names():
            if getattr(args, f"no_{name}", False):
                setattr(opts, name, False)

    return opts


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_patches:
        for name in core.PatchOptions().patch_names():
            print(f"  {name:30s} {core.PATCH_DESCRIPTIONS.get(name, '')}")
        return 0

    # No input + no --list-patches → assume GUI.
    if not args.input or args.gui:
        try:
            from . import gui
        except ImportError as e:
            print(f"GUI unavailable ({e}). Install python3-tk and retry, or "
                  f"pass an input image for headless mode.", file=sys.stderr)
            return 2
        gui.run()
        return 0

    log = setup_logging(args.verbose)

    if args.show_partitions:
        core.detect_layout(Path(args.input), log)
        return 0

    opts = options_from_args(args)

    if args.inspect:
        try:
            layout = core.detect_layout(opts.input_image, log)
            mounts = core.inspect(layout, log)
            tracker = mounts.pop("_tracker")
            print("\nMounted:")
            for k, v in mounts.items():
                print(f"  {k}: {v}")
            print("\nPress ENTER to unmount and exit.")
            try:
                input()
            except EOFError:
                pass
            tracker.cleanup(log)
            return 0
        except core.PatcherError as e:
            log.error("%s", e)
            return 1

    try:
        core.patch_image(opts, log)
        return 0
    except core.PatcherError as e:
        log.error("%s", e)
        return 1
    except KeyboardInterrupt:
        log.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

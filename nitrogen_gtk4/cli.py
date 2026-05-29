#!/usr/bin/env python3
"""
Command-line interface for nitrogen-gtk4.

Provides CLI options matching nitrogen's usage:
  --restore                 restore saved background
  --set-scaled  <file>      set background in scaled mode
  --set-centered <file>     set background centered
  --set-tiled   <file>      set background tiled
  --set-zoom    <file>      set background zoomed
  --set-zoom-fill <file>    set background zoomed-fill
  --set-auto    <file>      set background with automatic mode
  --save                    save current settings
  --head=<N>                specify monitor number
  --no-recurse              do not recurse into subdirectories (GUI)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

log = logging.getLogger("nitrogen_gtk4")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="nitrogen-gtk4",
        description="Modern GTK4 background manager for X11",
    )

    # Background-setting modes (mutually exclusive)
    mode_group = p.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--set-auto", metavar="FILE",
        help="Set background with automatic mode",
    )
    mode_group.add_argument(
        "--set-scaled", metavar="FILE",
        help="Set background in scaled mode",
    )
    mode_group.add_argument(
        "--set-centered", metavar="FILE",
        help="Set background centered",
    )
    mode_group.add_argument(
        "--set-tiled", metavar="FILE",
        help="Set background tiled",
    )
    mode_group.add_argument(
        "--set-zoom", metavar="FILE",
        help="Set background zoomed (fit, letterbox)",
    )
    mode_group.add_argument(
        "--set-zoom-fill", metavar="FILE",
        help="Set background zoomed-fill (cover, crop)",
    )
    mode_group.add_argument(
        "--restore", action="store_true",
        help="Restore background from saved configuration",
    )

    p.add_argument(
        "--head", type=int, default=0,
        help="Monitor/head number (default: 0)",
    )
    p.add_argument(
        "--save", action="store_true",
        help="Save current background settings to config",
    )
    p.add_argument(
        "--no-recurse", action="store_true",
        help="Do not recurse into subdirectories (GUI mode)",
    )
    p.add_argument(
        "--format-status", action="store_true",
        help="Print supported image format status and exit",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable verbose/debug logging",
    )
    p.add_argument(
        "--version", action="version",
        version="%(prog)s 1.0.2",
    )

    return p


def _set_background(file_path: str, mode_name: str, head: int, save: bool) -> int:
    """Set background and optionally save config. Returns exit code."""
    from nitrogen_gtk4.config import (
        HeadConfig, mode_id, load_bg_config, save_bg_config,
    )
    from nitrogen_gtk4.x11_backend import set_single_background

    file_path = os.path.abspath(file_path)
    if not os.path.isfile(file_path):
        log.error("File not found: %s", file_path)
        return 1

    mid = mode_id(mode_name)
    ok = set_single_background(file_path, mode=mid, head=head)
    if not ok:
        log.error("Failed to set background.")
        return 1

    if save:
        heads = load_bg_config()
        heads[head] = HeadConfig(file=file_path, mode=mid)
        save_bg_config(heads)
        log.info("Configuration saved.")

    # Always auto-save by default (like nitrogen)
    if not save:
        from nitrogen_gtk4.config import load_bg_config as lbc, save_bg_config as sbc
        heads = lbc()
        heads[head] = HeadConfig(file=file_path, mode=mid)
        sbc(heads)

    return 0


def _launch_gui() -> int:
    """Launch the GTK4 GUI."""
    try:
        from nitrogen_gtk4.gui import run_gui
        return run_gui()
    except ImportError as exc:
        log.error("GTK4 GUI not available: %s", exc)
        log.error("Make sure PyGObject and GTK4 are installed.")
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(name)s: %(levelname)s: %(message)s",
    )

    # --format-status
    if args.format_status:
        from nitrogen_gtk4.image_loader import format_status_text
        print(format_status_text())
        return 0

    # --restore
    if args.restore:
        from nitrogen_gtk4.x11_backend import restore_backgrounds
        return 0 if restore_backgrounds() else 1

    # --set-* modes
    mode_map = {
        "set_auto": ("auto", args.set_auto),
        "set_scaled": ("scaled", args.set_scaled),
        "set_centered": ("centered", args.set_centered),
        "set_tiled": ("tiled", args.set_tiled),
        "set_zoom": ("zoomed", args.set_zoom),
        "set_zoom_fill": ("zoomed-fill", args.set_zoom_fill),
    }

    for _attr, (mname, fpath) in mode_map.items():
        if fpath is not None:
            return _set_background(fpath, mname, args.head, args.save)

    # No CLI background action → launch GUI
    return _launch_gui()


if __name__ == "__main__":
    sys.exit(main())

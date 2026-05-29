"""
Configuration management — nitrogen-compatible config format.

Nitrogen stores its state in ``~/.config/nitrogen/bg-saved.cfg`` using an
INI-like format with ``[xin_N]`` sections (one per head/monitor).  Each
section contains:

    file=/path/to/background.jpg
    mode=N            (0=auto, 1=scaled, 2=centered, 3=tiled, 4=zoomed, 5=zoomed-fill)
    bgcolor=#000000

We also maintain a ``nitrogen.cfg`` for the list of background directories.
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(os.environ.get("NITROGEN_CONFIG_DIR", "~/.config/nitrogen")).expanduser()
BG_SAVED_FILE = CONFIG_DIR / "bg-saved.cfg"
DIRS_FILE = CONFIG_DIR / "nitrogen.cfg"

# Background display modes — numeric IDs match nitrogen's convention
MODE_AUTO = 0
MODE_SCALED = 1
MODE_CENTERED = 2
MODE_TILED = 3
MODE_ZOOMED = 4
MODE_ZOOMED_FILL = 5

MODE_NAMES = {
    MODE_AUTO: "auto",
    MODE_SCALED: "scaled",
    MODE_CENTERED: "centered",
    MODE_TILED: "tiled",
    MODE_ZOOMED: "zoomed",
    MODE_ZOOMED_FILL: "zoomed-fill",
}

MODE_BY_NAME = {v: k for k, v in MODE_NAMES.items()}
# Aliases
MODE_BY_NAME["zoom"] = MODE_ZOOMED
MODE_BY_NAME["fill"] = MODE_ZOOMED_FILL
MODE_BY_NAME["filled"] = MODE_ZOOMED_FILL


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class HeadConfig:
    """Background settings for a single monitor head."""
    file: str = ""
    mode: int = MODE_AUTO
    bgcolor: str = "#000000"


@dataclass
class AppConfig:
    """Overall application configuration."""
    heads: dict[int, HeadConfig] = field(default_factory=dict)
    directories: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_bg_config() -> dict[int, HeadConfig]:
    """Load per-head background settings from ``bg-saved.cfg``."""
    heads: dict[int, HeadConfig] = {}
    if not BG_SAVED_FILE.exists():
        log.info("No bg-saved.cfg found at %s", BG_SAVED_FILE)
        return heads

    cp = configparser.ConfigParser()
    cp.read(str(BG_SAVED_FILE))

    for section in cp.sections():
        # Sections are named  xin_0, xin_1, …
        if not section.startswith("xin_"):
            continue
        try:
            head_id = int(section.split("_", 1)[1])
        except (IndexError, ValueError):
            continue
        hc = HeadConfig(
            file=cp.get(section, "file", fallback=""),
            mode=cp.getint(section, "mode", fallback=MODE_AUTO),
            bgcolor=cp.get(section, "bgcolor", fallback="#000000"),
        )
        heads[head_id] = hc
    return heads


def save_bg_config(heads: dict[int, HeadConfig]) -> None:
    """Persist per-head background settings to ``bg-saved.cfg``."""
    _ensure_config_dir()
    cp = configparser.ConfigParser()
    for head_id in sorted(heads):
        hc = heads[head_id]
        section = f"xin_{head_id}"
        cp.add_section(section)
        cp.set(section, "file", hc.file)
        cp.set(section, "mode", str(hc.mode))
        cp.set(section, "bgcolor", hc.bgcolor)

    with open(BG_SAVED_FILE, "w") as fh:
        cp.write(fh)
    log.info("Saved bg config to %s", BG_SAVED_FILE)


def load_directories() -> list[str]:
    """Load the list of background directories from ``nitrogen.cfg``."""
    dirs: list[str] = []
    if not DIRS_FILE.exists():
        log.info("No nitrogen.cfg found at %s", DIRS_FILE)
        return dirs

    cp = configparser.ConfigParser()
    cp.read(str(DIRS_FILE))

    # nitrogen.cfg uses [nitrogen] section with dirs separated, or
    # numbered keys like dir_0, dir_1 …  We support both plus a simple
    # "dirs" key with newline-separated paths.
    if cp.has_section("nitrogen"):
        # Check for dirs key (newline-separated)
        raw_dirs = cp.get("nitrogen", "dirs", fallback="")
        if raw_dirs.strip():
            dirs.extend(d.strip() for d in raw_dirs.splitlines() if d.strip())
        # Also check numbered keys
        idx = 0
        while True:
            key = f"dir_{idx}"
            val = cp.get("nitrogen", key, fallback=None)
            if val is None:
                break
            dirs.append(val.strip())
            idx += 1

    # Also handle [geometry] sections from original nitrogen
    for section in cp.sections():
        if section.startswith("geometry"):
            pass  # not relevant for directory list

    return list(dict.fromkeys(dirs))  # deduplicate, preserve order


def save_directories(dirs: list[str]) -> None:
    """Persist the background directory list to ``nitrogen.cfg``."""
    _ensure_config_dir()
    cp = configparser.ConfigParser()
    cp.add_section("nitrogen")
    cp.set("nitrogen", "dirs", "\n" + "\n".join(dirs))

    with open(DIRS_FILE, "w") as fh:
        cp.write(fh)
    log.info("Saved directories config to %s", DIRS_FILE)


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def load_app_config() -> AppConfig:
    """Load both bg-saved and directory configs."""
    return AppConfig(
        heads=load_bg_config(),
        directories=load_directories(),
    )


def save_app_config(cfg: AppConfig) -> None:
    """Save both bg-saved and directory configs."""
    save_bg_config(cfg.heads)
    save_directories(cfg.directories)


def mode_name(mode_id: int) -> str:
    return MODE_NAMES.get(mode_id, "auto")


def mode_id(name: str) -> int:
    return MODE_BY_NAME.get(name.lower().strip(), MODE_AUTO)

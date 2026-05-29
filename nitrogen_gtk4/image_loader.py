"""
Image loading with extended format support.

Base:     Pillow  (JPEG, PNG, BMP, GIF, TIFF, WebP)
Optional: pillow-heif  (HEIF / HEIC)
          jxlpy        (JPEG XL)
          OpenEXR      (EXR — HDR / linear-float images)

All optional codecs are detected at import time with graceful fallback.
Use ``format_status()`` to check which codecs are available at runtime,
or run ``nitrogen-gtk4 --format-status`` from the command line.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Install hints — shown to users when a codec is missing
# ---------------------------------------------------------------------------

_INSTALL_HINTS: dict[str, str] = {
    "HEIF/HEIC": "pip install pillow-heif",
    "JPEG XL": "pip install jxlpy   (also needs: sudo apt install libjxl-dev)",
    "OpenEXR": "pip install OpenEXR",
}

# ---------------------------------------------------------------------------
# Optional codec registration
# ---------------------------------------------------------------------------

_HEIF_AVAILABLE = False
_JXL_AVAILABLE = False
_EXR_AVAILABLE = False
_EXR_HAS_NUMPY = False  # True if numpy is available for fast EXR path

try:
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _HEIF_AVAILABLE = True
    log.debug("HEIF/HEIC support enabled via pillow-heif %s",
              getattr(pillow_heif, "__version__", "?"))
except ImportError:
    log.debug("pillow-heif not installed — HEIF/HEIC support disabled")

try:
    import jxlpy  # type: ignore  # noqa: F401

    # Eagerly register the Pillow plugin so Image.open() handles .jxl
    try:
        from jxlpy import JXLImagePlugin  # noqa: F401
    except ImportError:
        pass
    _JXL_AVAILABLE = True
    log.debug("JPEG XL support enabled via jxlpy")
except ImportError:
    log.debug("jxlpy not installed — JPEG XL support disabled")

try:
    import OpenEXR  # type: ignore  # noqa: F401
    import Imath  # type: ignore  # noqa: F401

    _EXR_AVAILABLE = True
    log.debug("OpenEXR support enabled (v%s)",
              getattr(OpenEXR, "__version__", getattr(OpenEXR, "OPENEXR_VERSION", "?")))
    try:
        import numpy as _np  # noqa: F401

        _EXR_HAS_NUMPY = True
    except ImportError:
        log.debug("numpy not available — EXR loading will use the slow path")
except ImportError:
    log.debug("OpenEXR not installed — EXR support disabled")


# ---------------------------------------------------------------------------
# Supported extensions
# ---------------------------------------------------------------------------

# Extensions Pillow can always handle
_BASE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".tif", ".webp",
    ".ppm", ".pgm", ".pbm", ".pcx", ".tga", ".ico",
}

# Conditionally available
_HEIF_EXTENSIONS = {".heif", ".heic"}
_JXL_EXTENSIONS = {".jxl"}
_EXR_EXTENSIONS = {".exr"}


def supported_extensions() -> set[str]:
    """Return the set of lower-case file extensions we can load."""
    exts = set(_BASE_EXTENSIONS)
    if _HEIF_AVAILABLE:
        exts |= _HEIF_EXTENSIONS
    if _JXL_AVAILABLE:
        exts |= _JXL_EXTENSIONS
    if _EXR_AVAILABLE:
        exts |= _EXR_EXTENSIONS
    return exts


def is_supported(path: str | Path) -> bool:
    """Check whether a file's extension is a supported image format."""
    return Path(path).suffix.lower() in supported_extensions()


# ---------------------------------------------------------------------------
# EXR loader
#
# OpenEXR files store linear-float pixel data (typically 0.0–1.0 for SDR,
# or unbounded for HDR).  We need to:
#   1. Read the float channel data
#   2. Clamp to [0, 1] (simple tone-map for HDR — good enough for a
#      background preview; proper tone mapping is out of scope)
#   3. Convert to 8-bit sRGB
#
# We support two read paths:
#   • Fast path: OpenEXR v3 File API + numpy (vectorised, ~100× faster)
#   • Slow path: OpenEXR v1/v3 InputFile API + array module (no numpy)
# ---------------------------------------------------------------------------

def _load_exr(path: str) -> Optional[Image.Image]:
    """Load an OpenEXR file and return as a Pillow RGB Image."""
    if not _EXR_AVAILABLE:
        log.warning("Cannot load EXR file %s: OpenEXR library not installed. "
                     "Install with: %s", path, _INSTALL_HINTS["OpenEXR"])
        return None

    # Try the fast numpy path first, then fall back to the slow path
    img = None
    if _EXR_HAS_NUMPY:
        img = _load_exr_numpy(path)
    if img is None:
        img = _load_exr_slow(path)
    return img


def _load_exr_numpy(path: str) -> Optional[Image.Image]:
    """Fast EXR loader using numpy for vectorised float→uint8 conversion."""
    try:
        import numpy as np
        import OpenEXR
        import Imath

        exr_file = OpenEXR.InputFile(path)
        header = exr_file.header()
        dw = header["dataWindow"]
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1

        if width <= 0 or height <= 0:
            log.warning("EXR %s has invalid dimensions: %d×%d", path, width, height)
            return None

        # Read all channels as FLOAT (OpenEXR will convert HALF→FLOAT for us)
        pt = Imath.PixelType(Imath.PixelType.FLOAT)

        # Determine available channels
        ch_info = header.get("channels", {})
        has_r = "R" in ch_info
        has_g = "G" in ch_info
        has_b = "B" in ch_info
        has_y = "Y" in ch_info  # luminance-only EXR

        if has_r and has_g and has_b:
            # Standard RGB EXR
            r_raw = exr_file.channel("R", pt)
            g_raw = exr_file.channel("G", pt)
            b_raw = exr_file.channel("B", pt)

            r = np.frombuffer(r_raw, dtype=np.float32).reshape(height, width)
            g = np.frombuffer(g_raw, dtype=np.float32).reshape(height, width)
            b = np.frombuffer(b_raw, dtype=np.float32).reshape(height, width)

            log.debug("EXR %s: %d×%d RGB, R range [%.3f, %.3f]",
                      path, width, height, r.min(), r.max())

        elif has_y:
            # Luminance-only EXR — convert to greyscale RGB
            y_raw = exr_file.channel("Y", pt)
            y = np.frombuffer(y_raw, dtype=np.float32).reshape(height, width)
            r = g = b = y
            log.debug("EXR %s: %d×%d luminance (Y), range [%.3f, %.3f]",
                      path, width, height, y.min(), y.max())
        else:
            # Try to read whatever channels exist
            available = list(ch_info.keys())
            if len(available) >= 3:
                ch_names = available[:3]
            elif len(available) >= 1:
                ch_names = available[:1] * 3  # replicate single channel
            else:
                log.warning("EXR %s has no readable channels: %s", path, available)
                return None

            channels = []
            for ch_name in ch_names:
                raw = exr_file.channel(ch_name, pt)
                channels.append(np.frombuffer(raw, dtype=np.float32).reshape(height, width))
            r, g, b = channels[0], channels[1] if len(channels) > 1 else channels[0], \
                       channels[2] if len(channels) > 2 else channels[0]
            log.debug("EXR %s: %d×%d channels=%s", path, width, height, ch_names)

        exr_file.close()

        # Tone-map: clamp to [0, 1] and convert to 8-bit
        # For background display, simple clamping is appropriate.
        # (Reinhard or ACES tone mapping would be overkill here.)
        rgb = np.stack([r, g, b], axis=-1)  # (H, W, 3)
        rgb = np.clip(rgb, 0.0, 1.0)
        rgb_8bit = (rgb * 255.0 + 0.5).astype(np.uint8)

        return Image.fromarray(rgb_8bit, mode="RGB")

    except Exception as exc:
        log.debug("EXR numpy loader failed for %s: %s", path, exc)
        return None


def _load_exr_slow(path: str) -> Optional[Image.Image]:
    """Slow EXR loader using only the standard library (no numpy)."""
    try:
        import OpenEXR
        import Imath
        import array
        import struct

        exr_file = OpenEXR.InputFile(path)
        header = exr_file.header()
        dw = header["dataWindow"]
        width = dw.max.x - dw.min.x + 1
        height = dw.max.y - dw.min.y + 1

        if width <= 0 or height <= 0:
            log.warning("EXR %s has invalid dimensions: %d×%d", path, width, height)
            return None

        pt = Imath.PixelType(Imath.PixelType.FLOAT)
        ch_info = header.get("channels", {})

        # Pick channels to read
        if "R" in ch_info and "G" in ch_info and "B" in ch_info:
            ch_names = ("R", "G", "B")
        elif "Y" in ch_info:
            ch_names = ("Y", "Y", "Y")
        else:
            available = list(ch_info.keys())
            if not available:
                log.warning("EXR %s has no channels", path)
                return None
            ch_names = (available[0],) * 3

        channels = {}
        for ch_name in set(ch_names):
            raw = exr_file.channel(ch_name, pt)
            channels[ch_name] = array.array("f", raw)

        exr_file.close()

        # Build pixel data as bytes (much faster than putdata with tuples)
        pixel_count = width * height
        result = bytearray(pixel_count * 3)

        r_data = channels[ch_names[0]]
        g_data = channels[ch_names[1]]
        b_data = channels[ch_names[2]]

        for i in range(pixel_count):
            result[i * 3] = max(0, min(255, int(r_data[i] * 255.0 + 0.5)))
            result[i * 3 + 1] = max(0, min(255, int(g_data[i] * 255.0 + 0.5)))
            result[i * 3 + 2] = max(0, min(255, int(b_data[i] * 255.0 + 0.5)))

        img = Image.frombytes("RGB", (width, height), bytes(result))
        log.debug("EXR %s loaded via slow path: %d×%d", path, width, height)
        return img

    except Exception as exc:
        log.warning("Failed to load EXR %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# JXL loader (fallback if Pillow plugin not registered)
# ---------------------------------------------------------------------------

def _load_jxl(path: str) -> Optional[Image.Image]:
    """Load a JPEG XL file via jxlpy and return a Pillow Image."""
    if not _JXL_AVAILABLE:
        log.warning("Cannot load JXL file %s: jxlpy library not installed. "
                     "Install with: %s", path, _INSTALL_HINTS["JPEG XL"])
        return None
    try:
        from jxlpy import JXLImagePlugin  # noqa: F401 — registers the plugin

        return Image.open(path)
    except Exception:
        pass
    try:
        from jxlpy import JXLDecoder

        dec = JXLDecoder(path)
        return dec.get_image()
    except Exception as exc:
        log.warning("Failed to load JXL %s: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# Safety limits
# ---------------------------------------------------------------------------

# Maximum image dimension (width or height) we'll process.
# A 16384×16384 RGBA image takes ~1 GB of RAM — keep this reasonable.
MAX_DIMENSION = 16384

# Maximum file size we'll attempt to open (256 MB).
MAX_FILE_SIZE = 256 * 1024 * 1024


def _check_limits(path: str) -> Optional[str]:
    """Return an error string if the file exceeds safety limits, else None."""
    try:
        size = os.path.getsize(path)
        if size > MAX_FILE_SIZE:
            return f"File too large ({size / 1024 / 1024:.0f} MB > {MAX_FILE_SIZE / 1024 / 1024:.0f} MB limit)"
    except OSError as exc:
        return f"Cannot stat file: {exc}"
    return None


def _check_image_dimensions(img: Image.Image) -> Optional[str]:
    """Return an error string if the image is too large, else None."""
    w, h = img.size
    if w > MAX_DIMENSION or h > MAX_DIMENSION:
        return f"Image dimensions too large ({w}×{h}; max {MAX_DIMENSION})"
    if w <= 0 or h <= 0:
        return f"Invalid image dimensions ({w}×{h})"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_image(path: str | Path) -> Optional[Image.Image]:
    """
    Load an image from *path*, returning a Pillow ``Image`` in RGB(A) mode.

    Falls back through available codecs; returns ``None`` on failure.
    Enforces size/dimension safety limits.

    For unsupported or missing-codec formats, logs a WARNING with a clear
    message telling the user what package to install.
    """
    path = str(path)
    ext = os.path.splitext(path)[1].lower()

    if not os.path.isfile(path):
        log.warning("File does not exist: %s", path)
        return None

    # Check if the format is even recognized
    if ext not in supported_extensions() and ext not in _HEIF_EXTENSIONS | _JXL_EXTENSIONS | _EXR_EXTENSIONS:
        log.warning("Unsupported image format: %s (file: %s)", ext, path)
        return None

    # Check if the format needs an optional codec that's not installed
    if ext in _EXR_EXTENSIONS and not _EXR_AVAILABLE:
        log.warning("Cannot load %s: OpenEXR codec not available. Install with: %s",
                     path, _INSTALL_HINTS["OpenEXR"])
        return None
    if ext in _JXL_EXTENSIONS and not _JXL_AVAILABLE:
        log.warning("Cannot load %s: JPEG XL codec not available. Install with: %s",
                     path, _INSTALL_HINTS["JPEG XL"])
        return None
    if ext in _HEIF_EXTENSIONS and not _HEIF_AVAILABLE:
        log.warning("Cannot load %s: HEIF/HEIC codec not available. Install with: %s",
                     path, _INSTALL_HINTS["HEIF/HEIC"])
        return None

    # Pre-flight: check file size
    err = _check_limits(path)
    if err:
        log.warning("Skipping %s: %s", path, err)
        return None

    try:
        # EXR needs special handling (float data, not a Pillow format)
        if ext in _EXR_EXTENSIONS:
            log.debug("Loading EXR: %s", path)
            img = _load_exr(path)
            if img is not None:
                err = _check_image_dimensions(img)
                if err:
                    log.warning("Skipping %s: %s", path, err)
                    return None
                log.debug("Loaded EXR %s: %d×%d %s", path, img.width, img.height, img.mode)
            else:
                log.warning("Failed to load EXR: %s", path)
            return img

        # JXL: try Pillow first (plugin should be registered), then jxlpy
        if ext in _JXL_EXTENSIONS:
            log.debug("Loading JXL: %s", path)
            try:
                img = Image.open(path)
                img.load()
            except Exception:
                img = _load_jxl(path)
            if img is not None:
                err = _check_image_dimensions(img)
                if err:
                    log.warning("Skipping %s: %s", path, err)
                    return None
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA") if img.mode == "PA" else img.convert("RGB")
                log.debug("Loaded JXL %s: %d×%d %s", path, img.width, img.height, img.mode)
            else:
                log.warning("Failed to load JXL: %s", path)
            return img

        # General Pillow path (JPEG, PNG, WebP, HEIF via plugin, TIFF, BMP, …)
        log.debug("Loading via Pillow: %s", path)
        img = Image.open(path)
        img.load()  # Force full decode to catch truncated files

        err = _check_image_dimensions(img)
        if err:
            log.warning("Skipping %s: %s", path, err)
            return None

        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA") if "A" in img.mode or img.mode == "PA" else img.convert("RGB")

        log.debug("Loaded %s: %d×%d %s", path, img.width, img.height, img.mode)
        return img

    except Exception as exc:
        log.warning("Failed to load image %s: %s", path, exc)
        return None


def load_thumbnail(path: str | Path, size: tuple[int, int] = (256, 256)) -> Optional[Image.Image]:
    """Load an image and return a thumbnail no larger than *size*."""
    img = load_image(path)
    if img is None:
        return None
    img.thumbnail(size, Image.LANCZOS)
    return img


def format_status() -> dict[str, tuple[bool, str]]:
    """
    Return a dict mapping format families to ``(available, install_hint)``.

    *install_hint* is an empty string when the codec is available, or a
    pip command the user can run to install it.
    """
    return {
        "base (JPEG/PNG/BMP/GIF/TIFF/WebP)": (True, ""),
        "HEIF/HEIC (pillow-heif)": (_HEIF_AVAILABLE, _INSTALL_HINTS["HEIF/HEIC"]),
        "JPEG XL (jxlpy)": (_JXL_AVAILABLE, _INSTALL_HINTS["JPEG XL"]),
        "OpenEXR": (_EXR_AVAILABLE, _INSTALL_HINTS["OpenEXR"]),
    }


def format_status_text() -> str:
    """
    Return a human-readable multi-line string describing codec availability.

    Suitable for printing to the terminal or showing in a GUI dialog.
    """
    lines = ["Image format support:"]
    for fmt, (ok, hint) in format_status().items():
        if ok:
            lines.append(f"  \033[32m✓\033[0m {fmt}")
        else:
            lines.append(f"  \033[31m✗\033[0m {fmt}")
            if hint:
                lines.append(f"      Install: {hint}")
    return "\n".join(lines)

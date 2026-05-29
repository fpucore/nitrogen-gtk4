# nitrogen-gtk4

**Modern GTK4 rewrite of the [nitrogen](https://github.com/l3ib/nitrogen) background manager for X11 desktops.**

nitrogen-gtk4 replaces the aging, unmaintained GTK2-based nitrogen with a clean
Python + GTK4 rewrite while keeping full CLI and config-file compatibility.

---

## Features

| Feature | Details |
|---|---|
| **GTK4 thumbnail browser** | Browse backgrounds from multiple directories with async thumbnail loading |
| **Multi-monitor support** | Per-head background via RandR / Xinerama detection |
| **6 background modes** | auto · scaled · centered · tiled · zoomed (fit) · zoomed-fill (cover) |
| **Extended image formats** | JPEG, PNG, BMP, GIF, TIFF, WebP (Pillow) + optional HEIF/HEIC, JPEG XL, OpenEXR |
| **CLI compatible** | Drop-in `--restore`, `--set-*`, `--head=N` flags matching nitrogen |
| **Config compatible** | Reads/writes `~/.config/nitrogen/bg-saved.cfg` — same format as original nitrogen |
| **X11 native** | Sets `_XROOTPMAP_ID` / `ESETROOT_PMAP_ID` via python-xlib; pixmaps persist after exit |
| **Background colour** | Per-head background colour chooser (GUI) for letterbox/padding areas |
| **Preferences dialog** | GUI dialog for managing background directories |
| **Safety limits** | Max 16 384 × 16 384 px, max 256 MB file size — prevents runaway memory use |

---

## Requirements

- **Python** ≥ 3.10
- **GTK 4** runtime libraries
- **PyGObject** ≥ 3.42 (GObject introspection bindings for Python)
- **python-xlib** ≥ 0.33
- **Pillow** ≥ 10.0
- **X11 display server** (XLibre + Xorg supported), (Wayland not supported)

---

## Installation

### 1. Install system dependencies

GTK 4 and its GObject introspection bindings must be installed at the **system** level — they cannot be installed via pip.

#### Debian / Ubuntu

```bash
sudo apt install \
  python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
  libcairo2-dev libgirepository1.0-dev
```

#### Fedora

```bash
sudo dnf install \
  python3-gobject gtk4 cairo-gobject-devel gobject-introspection-devel
```

#### Arch Linux

```bash
sudo pacman -S python-gobject gtk4
```

### 2. Install nitrogen-gtk4

> **Important:** Because nitrogen-gtk4 depends on system-installed PyGObject
> (which cannot be pip-installed), you must use a virtual environment that has
> access to system site-packages.

```bash
# Create a venv that can see system-installed PyGObject
python3 -m venv --system-site-packages ~/.local/venvs/nitrogen-gtk4

# Activate it
source ~/.local/venvs/nitrogen-gtk4/bin/activate

# Install nitrogen-gtk4
pip install .

# Or for development (editable install)
pip install -e .
```

Alternatively, install directly into your user site-packages (no venv):

```bash
pip install --user .
```

### 3. Optional image format libraries

These must be installed **in the same Python environment** as nitrogen-gtk4.

```bash
# Inside the same venv or user site-packages:

# HEIF / HEIC support
pip install pillow-heif

# JPEG XL support  (also needs system library)
sudo apt install libjxl-dev          # Debian/Ubuntu
pip install jxlpy

# OpenEXR / HDR support
pip install OpenEXR

# Or install all optional codecs at once via extras
pip install ".[all]"
```

Individual extras are also available:

```bash
pip install ".[heif]"     # pillow-heif ≥ 0.13
pip install ".[jxl]"      # jxlpy ≥ 0.9
pip install ".[exr]"      # OpenEXR ≥ 3.0
```

#### System libraries for extended formats (Debian/Ubuntu)

```bash
sudo apt install libjxl-dev libwebp-dev libheif-dev libopenexr-dev
```

---

## Usage

### GUI mode

When launched without any `--set-*` or `--restore` flag, nitrogen-gtk4 opens a
graphical thumbnail browser:

```bash
nitrogen-gtk4
```

The GUI provides:

- **Thumbnail grid** — browse images from configured directories
- **Monitor selector** — choose which head/monitor to set the background on
- **Mode selector** — pick one of the 6 background display modes
- **Background colour** — choose a colour for padding/letterbox areas
- **Preferences** — add or remove background image directories
- **Apply / OK / Cancel** — apply immediately, apply-and-close, or cancel

### CLI mode

```bash
# Set background with automatic mode (zoomed-fill)
nitrogen-gtk4 --set-auto ~/Backgrounds/photo.jpg

# Other display modes
nitrogen-gtk4 --set-scaled ~/Backgrounds/photo.jpg
nitrogen-gtk4 --set-centered ~/Backgrounds/photo.jpg
nitrogen-gtk4 --set-tiled ~/Backgrounds/pattern.png
nitrogen-gtk4 --set-zoom ~/Backgrounds/photo.jpg        # fit inside, letterbox
nitrogen-gtk4 --set-zoom-fill ~/Backgrounds/photo.jpg   # cover, crop excess

# Target a specific monitor (0-indexed)
nitrogen-gtk4 --set-auto ~/Backgrounds/photo.jpg --head=1

# Restore saved background (for autostart scripts)
nitrogen-gtk4 --restore

# Explicitly save current settings (settings are also auto-saved on every set)
nitrogen-gtk4 --set-auto ~/Backgrounds/photo.jpg --save
```

### All CLI flags

```
nitrogen-gtk4 [OPTIONS]

Background modes (mutually exclusive):
  --set-auto FILE        Set background with automatic mode (zoomed-fill)
  --set-scaled FILE      Set background stretched to fill (ignores aspect ratio)
  --set-centered FILE    Set background centered at original size
  --set-tiled FILE       Set background tiled / repeated
  --set-zoom FILE        Set background zoomed to fit (letterbox)
  --set-zoom-fill FILE   Set background zoomed to cover (crop excess)
  --restore              Restore background from saved configuration

Options:
  --head N               Monitor/head number (default: 0)
  --save                 Explicitly save settings to config (also happens automatically)
  --no-recurse           Do not recurse into subdirectories when scanning (GUI mode)
  --format-status        Print supported image format status and exit

General:
  -v, --verbose          Enable verbose/debug logging
  --version              Show version number and exit
  -h, --help             Show help message and exit
```

### Check format support

```bash
nitrogen-gtk4 --format-status
```

Example output:

```
Image format support:
  ✓ base (JPEG/PNG/BMP/GIF/TIFF/WebP)
  ✓ HEIF/HEIC (pillow-heif)
  ✗ JPEG XL (jxlpy)
      Install: pip install jxlpy   (also needs: sudo apt install libjxl-dev)
  ✗ OpenEXR
      Install: pip install OpenEXR
```

### Autostart / session restore

Add to your `~/.xinitrc`, workspace autostart, or systemd user unit:

```bash
nitrogen-gtk4 --restore &
```

---

## Background Modes

| Mode ID | Name | CLI flag | Behaviour |
|---------|------|----------|-----------|
| 0 | auto | `--set-auto` | Automatic — currently behaves like zoomed-fill |
| 1 | scaled | `--set-scaled` | Stretch to fill the monitor (ignores aspect ratio) |
| 2 | centered | `--set-centered` | Place at original size in the centre; pad or crop |
| 3 | tiled | `--set-tiled` | Repeat the image across the monitor |
| 4 | zoomed | `--set-zoom` | Scale to fit inside the monitor (letterbox with bg colour) |
| 5 | zoomed-fill | `--set-zoom-fill` | Scale to cover the monitor (crop excess) |

---

## Configuration

Configuration files live in `~/.config/nitrogen/` for backwards compatibility
with the original nitrogen. 

Override this path with the `NITROGEN_CONFIG_DIR` environment variable.

### `bg-saved.cfg` — per-monitor background state

```ini
[xin_0]
file = /home/user/Backgrounds/photo.jpg
mode = 0
bgcolor = #0000

[xin_1]
file = /home/user/Backgrounds/landscape.png
mode = 5
bgcolor = #1a1a1a
```

Each `[xin_N]` section corresponds to a monitor head. Fields:

| Key | Description |
|-----|-------------|
| `file` | Absolute path to the background image |
| `mode` | Display mode (0–5, see table above) |
| `bgcolor` | Background/padding colour in hex (default `#0000`) |

> **Note:** Every `--set-*` CLI command automatically saves to this file.

### `nitrogen.cfg` — background directory list

```ini
[nitrogen]
dirs =
  /home/user/Backgrounds
  /usr/share/backgrounds
```

Directories can also be specified with numbered keys (`dir_0`, `dir_1`, …).

Both formats are supported; duplicates are removed automatically.

---

## Using with Blackbox-hwm and bb-setbg

Replace `nitrogen` calls in `bb-setbg` with `nitrogen-gtk4`:

```bash
# Before:  nitrogen --set-auto "$file" --head="$HEAD"
# After:   nitrogen-gtk4 --set-auto "$file" --head="$HEAD"

# Before:  nitrogen --restore
# After:   nitrogen-gtk4 --restore
```

---

## Supported Image Formats

#### Always available (via Pillow)

JPEG, PNG, BMP, GIF, TIFF, WebP, PPM, PGM, PBM, PCX, TGA, ICO

#### Optional (install separately)

| Format | Library | Install |
|--------|---------|---------|
| HEIF / HEIC | [pillow-heif](https://pypi.org/project/pillow-heif/) ≥ 0.13 | `pip install pillow-heif` |
| JPEG XL (.jxl) | [jxlpy](https://pypi.org/project/jxlpy/) ≥ 0.9 | `pip install jxlpy` + `sudo apt install libjxl-dev` |
| OpenEXR (.exr) | [OpenEXR](https://pypi.org/project/OpenEXR/) ≥ 3.0 | `pip install OpenEXR` |

OpenEXR files are tone-mapped from linear float data to 8-bit sRGB via simple clamping.

A fast NumPy path is used when available; otherwise a slower stdlib fallback is used 
automatically.

---

## Architecture

```
nitrogen_gtk4/
├── __init__.py        # Package metadata (__version__, __app_id__)
├── cli.py             # Argument parsing & CLI entry point
├── config.py          # Configuration read/write (nitrogen-compatible INI format)
├── gui.py             # GTK4 application window, thumbnail browser & preferences
├── image_loader.py    # Image loading with optional codec chain & safety limits
└── x11_backend.py     # X11 background setting via python-xlib (chunked put_image)
```

### Threading model

The GUI dispatches heavy work (image loading, X11 pixmap operations) to
background threads.

Results are marshalled back to the GTK main thread via
`GLib.idle_add()`.

All X11 backend functions are thread-safe.

### X11 pixmap lifecycle

Background pixmaps are retained on the X server via `SetCloseDownMode(RetainPermanent)`,
matching the behaviour of feh, hsetroot, and the original nitrogen.

Previous pixmaps are freed via `KillClient` before setting a new background to prevent
VRAM leaks.

Image data is written to the X server in row-chunks that respect the server's maximum 
request size, preventing the protocol-level hangs that affect naïve implementations.

---

## License

[GPL-2.0-or-later](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html) — matching original nitrogen.

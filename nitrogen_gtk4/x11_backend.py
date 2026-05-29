"""
X11 backend — set root-window background via python-xlib.

Strategy:
 1. Open the display.
 2. Convert the Pillow Image to the correct size for the target head.
 3. Create an X Pixmap, put the image data into it **in row-chunks** that
    respect the server's maximum request size.
 4. Set _XROOTPMAP_ID and ESETROOT_PMAP_ID atoms on the root window so
    compositors / panels can detect the background pixmap.
 5. Call SetCloseDownMode(RetainPermanent) so the pixmap persists on the
    X server after our client connection closes.
 6. Set the root window's background pixmap and clear it.

Multi-monitor: We composite each head's background onto a single
root-sized image (virtual screen), then set that image once.

PIXMAP LIFETIME (critical):
 By default, the X server destroys all resources created by a client when
 that client disconnects (close-down mode = DestroyAll).  This means the
 pixmap would be freed as soon as our Display.close() runs, leaving the
 _XROOTPMAP_ID / ESETROOT_PMAP_ID atoms pointing at a dead resource.

 The fix — identical to feh, hsetroot, xsetroot, and the original nitrogen
 — is to call SetCloseDownMode(RetainPermanent) before closing.  This tells
 the X server to keep the pixmap alive permanently until another client
 explicitly destroys it via KillClient.

 When we set a new background, we first KillClient on the old pixmap (read
 from the atoms) to free the previous server-side resource, then create the
 new one.

SAFETY:
 - All X operations are wrapped in try/except/finally so a failure never
   leaves the server in a grabbed or inconsistent state.
 - put_image is chunked to stay under the X protocol max-request-size
   (otherwise the server disconnects or hangs — the #1 freeze cause).
 - GCs are freed explicitly after use.
 - Display connections are always closed.
 - All functions are safe to call from any thread (main or background).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Optional

from PIL import Image

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Safe display helper
# ---------------------------------------------------------------------------

@contextmanager
def _open_display():
    """Context manager that opens and closes an X display connection."""
    from Xlib import display as xdisplay

    d = xdisplay.Display()
    try:
        yield d
    finally:
        try:
            d.flush()
            d.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Screen / head queries
# ---------------------------------------------------------------------------

def get_screen_size() -> tuple[int, int]:
    """Return (width, height) of the full virtual screen (root window)."""
    with _open_display() as d:
        screen = d.screen()
        return screen.width_in_pixels, screen.height_in_pixels


def get_head_geometries() -> list[dict]:
    """
    Return per-head geometries using RandR or Xinerama.

    Each entry: {"head": N, "x": …, "y": …, "width": …, "height": …}
    Falls back to the single full-screen geometry.
    """
    with _open_display() as d:
        heads: list[dict] = []

        # Try RandR first
        try:
            from Xlib.ext import randr as xrandr  # type: ignore

            root = d.screen().root
            res = xrandr.get_screen_resources(root)
            for i, output_id in enumerate(res.outputs):
                info = xrandr.get_output_info(root, output_id, res.config_timestamp)
                if info.crtc:
                    crtc = xrandr.get_crtc_info(root, info.crtc, res.config_timestamp)
                    heads.append({
                        "head": i,
                        "name": info.name,
                        "x": crtc.x,
                        "y": crtc.y,
                        "width": crtc.width,
                        "height": crtc.height,
                    })
            if heads:
                heads.sort(key=lambda h: (h["x"], h["y"]))
                for idx, h in enumerate(heads):
                    h["head"] = idx
                return heads
        except Exception as exc:
            log.debug("RandR query failed: %s", exc)

        # Try Xinerama
        try:
            from Xlib.ext import xinerama  # type: ignore

            if d.has_extension("XINERAMA"):
                screens = xinerama.query_screens(d).screens  # type: ignore
                for i, s in enumerate(screens):
                    heads.append({
                        "head": i,
                        "name": f"xin_{i}",
                        "x": s.x,
                        "y": s.y,
                        "width": s.width,
                        "height": s.height,
                    })
                if heads:
                    return heads
        except Exception as exc:
            log.debug("Xinerama query failed: %s", exc)

        # Fallback: single screen
        screen = d.screen()
        return [{
            "head": 0,
            "name": "default",
            "x": 0,
            "y": 0,
            "width": screen.width_in_pixels,
            "height": screen.height_in_pixels,
        }]


# ---------------------------------------------------------------------------
# Image placement modes
# ---------------------------------------------------------------------------

def _place_image(
    img: Image.Image,
    target_w: int,
    target_h: int,
    mode: int,
    bgcolor: str = "#000000",
) -> Image.Image:
    """
    Place *img* into a (target_w × target_h) canvas according to *mode*.

    Modes (matching nitrogen IDs):
        0 = auto        — choose best fit (zoomed-fill)
        1 = scaled      — stretch to fill (ignores aspect ratio)
        2 = centered    — center at original size, crop or pad
        3 = tiled       — tile the image
        4 = zoomed      — scale to fit inside, letterbox
        5 = zoomed-fill — scale to cover, crop excess
    """
    from nitrogen_gtk4.config import (
        MODE_AUTO, MODE_SCALED, MODE_CENTERED,
        MODE_TILED, MODE_ZOOMED, MODE_ZOOMED_FILL,
    )

    canvas = Image.new("RGB", (target_w, target_h), bgcolor)
    iw, ih = img.size

    if mode == MODE_SCALED:
        canvas = img.resize((target_w, target_h), Image.LANCZOS).convert("RGB")

    elif mode == MODE_CENTERED:
        x = (target_w - iw) // 2
        y = (target_h - ih) // 2
        canvas.paste(img, (x, y))

    elif mode == MODE_TILED:
        for ty in range(0, target_h, ih):
            for tx in range(0, target_w, iw):
                canvas.paste(img, (tx, ty))

    elif mode == MODE_ZOOMED:
        ratio = min(target_w / iw, target_h / ih)
        new_w = int(iw * ratio)
        new_h = int(ih * ratio)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        x = (target_w - new_w) // 2
        y = (target_h - new_h) // 2
        canvas.paste(resized, (x, y))

    elif mode == MODE_ZOOMED_FILL:
        ratio = max(target_w / iw, target_h / ih)
        new_w = int(iw * ratio)
        new_h = int(ih * ratio)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        x = (new_w - target_w) // 2
        y = (new_h - target_h) // 2
        canvas = resized.crop((x, y, x + target_w, y + target_h)).convert("RGB")

    else:  # MODE_AUTO
        ratio = max(target_w / iw, target_h / ih)
        new_w = int(iw * ratio)
        new_h = int(ih * ratio)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        x = (new_w - target_w) // 2
        y = (new_h - target_h) // 2
        canvas = resized.crop((x, y, x + target_w, y + target_h)).convert("RGB")

    return canvas


# ---------------------------------------------------------------------------
# Chunked put_image — the critical fix for the freeze
# ---------------------------------------------------------------------------

def _put_image_chunked(pixmap, gc, image_data: bytes, width: int, height: int,
                       depth: int, max_request_bytes: int) -> None:
    """
    Write *image_data* (BGRA, ZPixmap format) into *pixmap* in row-chunks
    that each fit within the X server's maximum request size.

    A single huge put_image request (e.g. 8 MB for 1920×1080×4) exceeds the
    default X max-request-size (~4 MB on most servers).  If the request is
    larger the server disconnects or hangs — this was THE primary freeze cause.
    """
    from Xlib import X

    bytes_per_pixel = 4  # BGRA
    row_bytes = width * bytes_per_pixel

    # Leave generous room for the X request header (64 bytes is overkill but safe)
    usable = max_request_bytes - 64
    if usable < row_bytes:
        # Absolute minimum: we must be able to send at least one row
        usable = row_bytes

    rows_per_chunk = max(1, usable // row_bytes)

    y_offset = 0
    while y_offset < height:
        chunk_rows = min(rows_per_chunk, height - y_offset)
        start = y_offset * row_bytes
        end = start + chunk_rows * row_bytes
        chunk_data = image_data[start:end]

        pixmap.put_image(
            gc,
            0, y_offset,             # dst x, y
            width, chunk_rows,       # width, height of this chunk
            X.ZPixmap,
            depth,
            0,                       # left_pad
            chunk_data,
        )
        y_offset += chunk_rows


# ---------------------------------------------------------------------------
# Free old root pixmap
# ---------------------------------------------------------------------------

def _free_old_root_pixmap(d, root) -> None:
    """
    Free the previous root-window background pixmap via XKillClient.

    This is the standard approach used by feh, hsetroot, and the original
    nitrogen.  XKillClient with a pixmap ID destroys all resources belonging
    to the client that created that pixmap.

    We read the pixmap ID from ``_XROOTPMAP_ID`` and/or ``ESETROOT_PMAP_ID``
    atoms on the *caller's* connection ``d``, but perform the actual
    KillClient on a **separate disposable connection**.  This is necessary
    because python-xlib's error handler for certain extensions (e.g. RandR)
    has bugs that can corrupt the connection state if KillClient triggers an
    unexpected error.  Using a separate connection isolates that damage.
    """
    from Xlib import Xatom
    from Xlib import display as xdisplay
    from Xlib.xobject.drawable import Pixmap as XPixmap

    try:
        old_ids = set()

        for atom_name in ("_XROOTPMAP_ID", "ESETROOT_PMAP_ID"):
            atom = d.intern_atom(atom_name, only_if_exists=True)
            if atom is None or atom == 0:
                continue
            prop = root.get_full_property(atom, Xatom.PIXMAP)
            if prop and prop.value:
                pid = prop.value[0]
                if pid:
                    old_ids.add(pid)

        if not old_ids:
            return

        # Open a separate throwaway connection for KillClient
        d2 = None
        try:
            d2 = xdisplay.Display()
            for old_id in old_ids:
                try:
                    old_pixmap = XPixmap(d2.display, old_id)
                    old_pixmap.kill_client(onerror=lambda *a: None)
                    log.debug("KillClient on old root pixmap 0x%x", old_id)
                except Exception:
                    pass  # not ours, already dead, or no permissions
            try:
                d2.flush()
            except Exception:
                pass
        finally:
            if d2 is not None:
                try:
                    d2.close()
                except Exception:
                    pass

    except Exception as exc:
        log.debug("Could not free old root pixmap: %s", exc)


# ---------------------------------------------------------------------------
# Set background on X11 — the main entry point
# ---------------------------------------------------------------------------

def set_background(
    images: dict[int, tuple[Image.Image, int, str]],
) -> bool:
    """
    Set background for one or more heads.

    *images* maps head-index → (PIL Image, mode_int, bgcolor_hex).
    Returns True on success.

    The created pixmap is retained on the X server permanently (via
    ``SetCloseDownMode(RetainPermanent)``) so it survives after our
    Display connection closes and our process exits.  This is how all
    X11 background setters work (feh, hsetroot, nitrogen).

    This function is safe to call from any thread (main or background).
    """
    from Xlib import X, Xatom
    from Xlib import display as xdisplay

    heads = get_head_geometries()

    d = None
    pixmap = None
    gc = None
    success = False

    try:
        d = xdisplay.Display()
        screen = d.screen()
        root = screen.root
        root_w = screen.width_in_pixels
        root_h = screen.height_in_pixels
        depth = screen.root_depth

        # --- Composite image for the full virtual screen -------------------
        composite = Image.new("RGB", (root_w, root_h), "#000000")

        for head in heads:
            hid = head["head"]
            if hid not in images:
                continue
            img, mode, bgcolor = images[hid]
            placed = _place_image(img, head["width"], head["height"], mode, bgcolor)
            composite.paste(placed, (head["x"], head["y"]))

        # Convert to BGRA bytes (X11 ZPixmap with 32-bit depth)
        rgba = composite.convert("RGBA")
        raw = rgba.tobytes("raw", "BGRA")

        # Free PIL images early to reclaim memory
        del composite, rgba

        # --- X11 operations ------------------------------------------------

        # Query server's maximum request length (in 4-byte units)
        max_request_len = d.display.info.max_request_length  # in 4-byte units
        max_request_bytes = max_request_len * 4
        log.debug("X server max request length: %d bytes", max_request_bytes)

        # Free old retained pixmap to prevent VRAM leak.
        # This sends KillClient to destroy the resources of the client
        # that created the previous background pixmap.
        _free_old_root_pixmap(d, root)

        # Create new pixmap and GC
        pixmap = root.create_pixmap(root_w, root_h, depth)
        gc = root.create_gc()

        log.debug("Created pixmap 0x%x (%d×%d, depth %d)",
                  pixmap.id, root_w, root_h, depth)

        # Write image data in safe chunks
        _put_image_chunked(pixmap, gc, raw, root_w, root_h, depth, max_request_bytes)

        # Free raw data now that it's on the server
        del raw

        # Free the GC — we no longer need it
        gc.free()
        gc = None

        # Set root window background
        root.change_attributes(background_pixmap=pixmap)
        root.clear_area(0, 0, root_w, root_h, False)

        # Advertise pixmap via standard atoms so compositors, panels, and
        # other background tools (like nitrogen --restore) can find it.
        _xrootpmap = d.intern_atom("_XROOTPMAP_ID")
        _esetroot = d.intern_atom("ESETROOT_PMAP_ID")
        root.change_property(_xrootpmap, Xatom.PIXMAP, 32, [pixmap.id])
        root.change_property(_esetroot, Xatom.PIXMAP, 32, [pixmap.id])

        # *** CRITICAL: Tell the X server to keep our pixmap alive ***
        #
        # Without this, Display.close() triggers DestroyAll (the default
        # close-down mode), which destroys the pixmap — leaving the atoms
        # pointing at a dead resource (BadDrawable).
        #
        # RetainPermanent tells the server: "when I disconnect, do NOT
        # destroy my resources.  Keep them alive until another client
        # explicitly calls KillClient on them."
        #
        # This is exactly what XSetCloseDownMode(dpy, RetainPermanent)
        # does in C, and it's how feh, hsetroot, and nitrogen all work.
        d.set_close_down_mode(X.RetainPermanent)

        d.flush()
        d.sync()

        success = True
        log.info("Background set successfully (%d×%d, %d heads, pixmap 0x%x retained)",
                 root_w, root_h, len(images), pixmap.id)
        return True

    except Exception as exc:
        log.error("Failed to set background: %s", exc, exc_info=True)
        return False
    finally:
        # Clean up the GC (always)
        if gc is not None:
            try:
                gc.free()
            except Exception:
                pass

        # Free the pixmap ONLY on failure.
        # On success the pixmap must persist (RetainPermanent keeps it).
        if not success and pixmap is not None:
            try:
                pixmap.free()
            except Exception:
                pass

        # Always close the display connection
        if d is not None:
            try:
                d.flush()
                d.close()
            except Exception:
                pass


def set_single_background(
    image_path: str,
    mode: int = 0,
    head: int = 0,
    bgcolor: str = "#000000",
) -> bool:
    """Convenience: load one image and set it on the given head."""
    from nitrogen_gtk4.image_loader import load_image

    img = load_image(image_path)
    if img is None:
        log.error("Could not load image: %s", image_path)
        return False
    return set_background({head: (img, mode, bgcolor)})


def restore_backgrounds() -> bool:
    """Restore backgrounds from the saved config."""
    from nitrogen_gtk4.config import load_bg_config
    from nitrogen_gtk4.image_loader import load_image

    heads_cfg = load_bg_config()
    if not heads_cfg:
        log.warning("No saved background configuration found.")
        return False

    images: dict[int, tuple[Image.Image, int, str]] = {}
    for hid, hc in heads_cfg.items():
        if not hc.file:
            continue
        img = load_image(hc.file)
        if img is None:
            log.warning("Could not load saved background for head %d: %s", hid, hc.file)
            continue
        images[hid] = (img, hc.mode, hc.bgcolor)

    if not images:
        log.warning("No valid backgrounds to restore.")
        return False

    return set_background(images)

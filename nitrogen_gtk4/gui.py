"""
GTK4 graphical interface for nitrogen-gtk4.

Features:
  • Thumbnail browser showing backgrounds from selected directories
  • Multi-monitor / head selection
  • Background mode selector (auto, scaled, centered, tiled, zoomed, zoomed-fill)
  • Background colour chooser
  • Preferences dialog for directory management
  • Apply / OK / Cancel buttons

Threading model:
  GTK must only be touched from the main thread.  Heavy work (image loading,
  X11 background setting) is dispatched to background threads and results are
  marshalled back via ``GLib.idle_add``.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from nitrogen_gtk4 import __app_id__, __version__
from nitrogen_gtk4.config import (
    MODE_AUTO, MODE_BY_NAME, MODE_NAMES,
    HeadConfig, load_app_config, save_app_config, AppConfig,
    mode_id, mode_name,
)
from nitrogen_gtk4.image_loader import is_supported, load_thumbnail, supported_extensions
from nitrogen_gtk4.x11_backend import get_head_geometries, set_background

log = logging.getLogger(__name__)

THUMB_SIZE = (192, 128)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pil_to_pixbuf(pil_img) -> tuple[GdkPixbuf.Pixbuf, bytes]:
    """
    Convert a Pillow Image to a GdkPixbuf.

    Returns (pixbuf, data_bytes).  The caller MUST keep a reference to
    *data_bytes* for at least as long as the pixbuf is alive, because
    ``new_from_data`` does **not** copy the buffer.
    """
    from PIL import Image

    if pil_img.mode != "RGB":
        pil_img = pil_img.convert("RGB")
    w, h = pil_img.size
    data = pil_img.tobytes()
    pixbuf = GdkPixbuf.Pixbuf.new_from_data(
        data, GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3,
    )
    return pixbuf, data


def _pil_to_texture(pil_img) -> tuple[Gdk.Texture, bytes]:
    """
    Convert a Pillow Image to a Gdk.Texture (GTK4).

    Returns (texture, backing_bytes) — caller must hold backing_bytes.
    """
    pixbuf, data = _pil_to_pixbuf(pil_img)
    texture = Gdk.Texture.new_for_pixbuf(pixbuf)
    return texture, data


def _collect_images(directories: list[str], recurse: bool = True) -> list[str]:
    """Collect all supported image paths from the given directories."""
    paths: list[str] = []
    exts = supported_extensions()
    for d in directories:
        dp = Path(d).expanduser()
        if not dp.is_dir():
            continue
        if recurse:
            for f in sorted(dp.rglob("*")):
                if f.is_file() and f.suffix.lower() in exts:
                    paths.append(str(f))
        else:
            for f in sorted(dp.iterdir()):
                if f.is_file() and f.suffix.lower() in exts:
                    paths.append(str(f))
    return paths


# ---------------------------------------------------------------------------
# Preferences Dialog
# ---------------------------------------------------------------------------

class PreferencesDialog(Gtk.Window):
    """Dialog for managing background directories."""

    def __init__(self, parent: Gtk.Window, directories: list[str], callback):
        super().__init__(
            title="Preferences — Background Directories",
            transient_for=parent,
            modal=True,
            default_width=500,
            default_height=350,
        )
        self._callback = callback
        self._dirs = list(directories)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        vbox.set_margin_top(12)
        vbox.set_margin_bottom(12)
        vbox.set_margin_start(12)
        vbox.set_margin_end(12)
        self.set_child(vbox)

        label = Gtk.Label(label="<b>Background Directories</b>", use_markup=True)
        label.set_halign(Gtk.Align.START)
        vbox.append(label)

        # List
        self._store = Gtk.StringList()
        for d in self._dirs:
            self._store.append(d)

        self._selection = Gtk.SingleSelection(model=self._store)
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        self._listview = Gtk.ListView(model=self._selection, factory=factory)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_child(self._listview)
        vbox.append(scroll)

        # Buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn_box.set_halign(Gtk.Align.END)

        btn_add = Gtk.Button(label="Add…")
        btn_add.connect("clicked", self._on_add)
        btn_remove = Gtk.Button(label="Remove")
        btn_remove.connect("clicked", self._on_remove)
        btn_close = Gtk.Button(label="Close")
        btn_close.connect("clicked", lambda _: self._finish())

        btn_box.append(btn_add)
        btn_box.append(btn_remove)
        btn_box.append(btn_close)
        vbox.append(btn_box)

    @staticmethod
    def _on_factory_setup(_factory, list_item):
        label = Gtk.Label(xalign=0)
        label.set_margin_start(8)
        label.set_margin_end(8)
        label.set_margin_top(4)
        label.set_margin_bottom(4)
        list_item.set_child(label)

    @staticmethod
    def _on_factory_bind(_factory, list_item):
        label = list_item.get_child()
        item = list_item.get_item()
        label.set_text(item.get_string())

    def _on_add(self, _btn):
        dialog = Gtk.FileDialog(title="Select Background Directory")
        dialog.select_folder(self, None, self._on_folder_selected)

    def _on_folder_selected(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
            if folder:
                path = folder.get_path()
                if path and path not in self._dirs:
                    self._dirs.append(path)
                    self._store.append(path)
        except GLib.Error:
            pass

    def _on_remove(self, _btn):
        idx = self._selection.get_selected()
        if idx < len(self._dirs):
            self._dirs.pop(idx)
            self._store.remove(idx)

    def _finish(self):
        self._callback(list(self._dirs))
        self.close()


# ---------------------------------------------------------------------------
# Main Application Window
# ---------------------------------------------------------------------------

class MainWindow(Gtk.ApplicationWindow):
    """Main nitrogen-gtk4 window with thumbnail browser."""

    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="nitrogen-gtk4", default_width=860, default_height=620)
        self._cfg = load_app_config()
        self._heads = get_head_geometries()
        self._selected_file: Optional[str] = None
        # Keep references to backing byte-buffers so GC doesn't collect them
        # while textures are still displayed.
        self._thumb_data_refs: dict[str, bytes] = {}
        self._applying = False  # guard against double-click

        self._build_ui()
        self._populate_thumbnails()

    # ---- UI construction --------------------------------------------------

    def _build_ui(self):
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(vbox)

        # Header bar
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        prefs_btn = Gtk.Button(icon_name="preferences-system-symbolic")
        prefs_btn.set_tooltip_text("Preferences (directories)")
        prefs_btn.connect("clicked", self._on_prefs)
        header.pack_start(prefs_btn)

        about_btn = Gtk.Button(icon_name="help-about-symbolic")
        about_btn.set_tooltip_text("About")
        about_btn.connect("clicked", self._on_about)
        header.pack_end(about_btn)

        # --- Controls bar ---
        ctrl = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        ctrl.set_margin_top(8)
        ctrl.set_margin_bottom(8)
        ctrl.set_margin_start(12)
        ctrl.set_margin_end(12)
        vbox.append(ctrl)

        # Head selector
        ctrl.append(Gtk.Label(label="Monitor:"))
        self._head_combo = Gtk.DropDown.new_from_strings(
            [f"Head {h['head']}: {h.get('name', '')} ({h['width']}×{h['height']})" for h in self._heads]
        )
        self._head_combo.set_selected(0)
        ctrl.append(self._head_combo)

        # Mode selector
        ctrl.append(Gtk.Label(label="Mode:"))
        mode_strings = [MODE_NAMES[k] for k in sorted(MODE_NAMES)]
        self._mode_combo = Gtk.DropDown.new_from_strings(mode_strings)
        self._mode_combo.set_selected(0)
        ctrl.append(self._mode_combo)

        # BG colour
        ctrl.append(Gtk.Label(label="BG:"))
        self._color_btn = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog())
        rgba = Gdk.RGBA()
        rgba.parse("#000000")
        self._color_btn.set_rgba(rgba)
        ctrl.append(self._color_btn)

        # Spacer
        spacer = Gtk.Box(hexpand=True)
        ctrl.append(spacer)

        # Selected file label
        self._file_label = Gtk.Label(label="(none selected)")
        self._file_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self._file_label.set_max_width_chars(40)
        ctrl.append(self._file_label)

        # --- Thumbnail grid (scrollable) ---
        scroll = Gtk.ScrolledWindow(vexpand=True)
        vbox.append(scroll)

        self._flowbox = Gtk.FlowBox()
        self._flowbox.set_valign(Gtk.Align.START)
        self._flowbox.set_max_children_per_line(20)
        self._flowbox.set_min_children_per_line(3)
        self._flowbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._flowbox.set_homogeneous(True)
        self._flowbox.connect("child-activated", self._on_thumb_activated)
        scroll.set_child(self._flowbox)

        # --- Bottom button bar ---
        btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_bar.set_margin_top(8)
        btn_bar.set_margin_bottom(12)
        btn_bar.set_margin_start(12)
        btn_bar.set_margin_end(12)
        btn_bar.set_halign(Gtk.Align.END)
        vbox.append(btn_bar)

        self._btn_apply = Gtk.Button(label="Apply")
        self._btn_apply.add_css_class("suggested-action")
        self._btn_apply.connect("clicked", self._on_apply)

        self._btn_ok = Gtk.Button(label="OK")
        self._btn_ok.connect("clicked", self._on_ok)

        btn_cancel = Gtk.Button(label="Cancel")
        btn_cancel.connect("clicked", lambda _: self.close())

        btn_bar.append(self._btn_apply)
        btn_bar.append(self._btn_ok)
        btn_bar.append(btn_cancel)

    # ---- Thumbnail population ---------------------------------------------

    def _populate_thumbnails(self):
        # Clear existing
        while True:
            child = self._flowbox.get_first_child()
            if child is None:
                break
            self._flowbox.remove(child)
        self._thumb_data_refs.clear()

        dirs = self._cfg.directories
        if not dirs:
            placeholder = Gtk.Label(label="No background directories configured.\nOpen Preferences to add directories.")
            placeholder.set_margin_top(40)
            self._flowbox.append(placeholder)
            return

        images = _collect_images(dirs)
        if not images:
            placeholder = Gtk.Label(label="No supported images found in the configured directories.")
            placeholder.set_margin_top(40)
            self._flowbox.append(placeholder)
            return

        for img_path in images:
            child = self._create_thumb_widget(img_path)
            self._flowbox.append(child)
            threading.Thread(target=self._load_thumb_async, args=(img_path, child), daemon=True).start()

    def _create_thumb_widget(self, img_path: str) -> Gtk.Box:
        """Create a thumbnail box with placeholder."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box._image_path = img_path  # stash for later

        picture = Gtk.Picture()
        picture.set_size_request(THUMB_SIZE[0], THUMB_SIZE[1])
        picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        box.append(picture)
        box._picture = picture

        name = Path(img_path).name
        label = Gtk.Label(label=name)
        label.set_ellipsize(3)
        label.set_max_width_chars(22)
        label.set_tooltip_text(img_path)
        box.append(label)

        return box

    def _load_thumb_async(self, img_path: str, box: Gtk.Box):
        """Load thumbnail in a worker thread, update widget on main thread."""
        try:
            thumb = load_thumbnail(img_path, THUMB_SIZE)
            if thumb is None:
                return
            texture, data = _pil_to_texture(thumb)
            # Schedule UI update on main thread
            GLib.idle_add(self._set_thumb_texture, box, texture, data, img_path)
        except Exception as exc:
            log.debug("Thumbnail load failed for %s: %s", img_path, exc)

    def _set_thumb_texture(self, box: Gtk.Box, texture: Gdk.Texture,
                           data: bytes, img_path: str):
        """Called on the GTK main thread to update a thumbnail widget."""
        # Keep backing data alive
        self._thumb_data_refs[img_path] = data
        try:
            picture = box._picture
            picture.set_paintable(texture)
        except Exception:
            pass
        return False  # remove from idle queue

    # ---- Callbacks --------------------------------------------------------

    def _on_thumb_activated(self, _flowbox, child):
        box = child.get_child()
        path = getattr(box, "_image_path", None)
        if path:
            self._selected_file = path
            self._file_label.set_text(Path(path).name)
            self._file_label.set_tooltip_text(path)

    def _get_current_mode_id(self) -> int:
        idx = self._mode_combo.get_selected()
        keys = sorted(MODE_NAMES.keys())
        return keys[idx] if idx < len(keys) else MODE_AUTO

    def _get_current_head(self) -> int:
        idx = self._head_combo.get_selected()
        if idx < len(self._heads):
            return self._heads[idx]["head"]
        return 0

    def _get_bgcolor_hex(self) -> str:
        rgba = self._color_btn.get_rgba()
        r = int(rgba.red * 255)
        g = int(rgba.green * 255)
        b = int(rgba.blue * 255)
        return f"#{r:02x}{g:02x}{b:02x}"

    def _set_buttons_sensitive(self, sensitive: bool):
        """Enable/disable Apply and OK buttons."""
        self._btn_apply.set_sensitive(sensitive)
        self._btn_ok.set_sensitive(sensitive)

    def _on_apply(self, _btn):
        if self._applying:
            return  # prevent double-click
        if not self._selected_file:
            self._show_message("No background selected.")
            return

        # Gather parameters on the main thread
        selected = self._selected_file
        head = self._get_current_head()
        mid = self._get_current_mode_id()
        bgcolor = self._get_bgcolor_hex()

        # Disable buttons while applying
        self._applying = True
        self._set_buttons_sensitive(False)
        self._file_label.set_text("Applying…")

        # Run the heavy X11 work in a background thread
        threading.Thread(
            target=self._apply_worker,
            args=(selected, head, mid, bgcolor),
            daemon=True,
        ).start()

    def _apply_worker(self, file_path: str, head: int, mid: int, bgcolor: str):
        """Background thread: load image and set background via X11."""
        from nitrogen_gtk4.image_loader import load_image

        try:
            img = load_image(file_path)
            if img is None:
                GLib.idle_add(self._apply_done, False, file_path, head, mid, bgcolor,
                              f"Could not load image:\n{file_path}")
                return

            ok = set_background({head: (img, mid, bgcolor)})
            del img  # free memory

            if ok:
                GLib.idle_add(self._apply_done, True, file_path, head, mid, bgcolor, "")
            else:
                GLib.idle_add(self._apply_done, False, file_path, head, mid, bgcolor,
                              "Failed to set background.\nCheck the log for X11 errors.")
        except Exception as exc:
            GLib.idle_add(self._apply_done, False, file_path, head, mid, bgcolor,
                          f"Error: {exc}")

    def _apply_done(self, success: bool, file_path: str, head: int, mid: int,
                    bgcolor: str, error_msg: str):
        """Called on the main thread when the apply worker finishes."""
        self._applying = False
        self._set_buttons_sensitive(True)
        self._file_label.set_text(Path(file_path).name)

        if success:
            self._cfg.heads[head] = HeadConfig(
                file=file_path, mode=mid, bgcolor=bgcolor,
            )
            save_app_config(self._cfg)
        else:
            self._show_message(error_msg)

        return False  # remove from idle queue

    def _on_ok(self, btn):
        if self._applying:
            return
        if not self._selected_file:
            self._show_message("No background selected.")
            return

        # Gather parameters
        selected = self._selected_file
        head = self._get_current_head()
        mid = self._get_current_mode_id()
        bgcolor = self._get_bgcolor_hex()

        self._applying = True
        self._set_buttons_sensitive(False)
        self._file_label.set_text("Applying…")

        def _ok_worker():
            from nitrogen_gtk4.image_loader import load_image
            try:
                img = load_image(selected)
                if img is None:
                    GLib.idle_add(self._ok_done, False, selected, head, mid, bgcolor,
                                  f"Could not load image:\n{selected}")
                    return
                ok = set_background({head: (img, mid, bgcolor)})
                del img
                if ok:
                    GLib.idle_add(self._ok_done, True, selected, head, mid, bgcolor, "")
                else:
                    GLib.idle_add(self._ok_done, False, selected, head, mid, bgcolor,
                                  "Failed to set background.")
            except Exception as exc:
                GLib.idle_add(self._ok_done, False, selected, head, mid, bgcolor,
                              f"Error: {exc}")

        threading.Thread(target=_ok_worker, daemon=True).start()

    def _ok_done(self, success: bool, file_path: str, head: int, mid: int,
                 bgcolor: str, error_msg: str):
        """Called on main thread when OK worker finishes."""
        self._applying = False
        self._set_buttons_sensitive(True)

        if success:
            self._cfg.heads[head] = HeadConfig(
                file=file_path, mode=mid, bgcolor=bgcolor,
            )
            save_app_config(self._cfg)
            self.close()
        else:
            self._file_label.set_text(Path(file_path).name)
            self._show_message(error_msg)

        return False

    def _on_prefs(self, _btn):
        dlg = PreferencesDialog(self, self._cfg.directories, self._on_dirs_changed)
        dlg.present()

    def _on_dirs_changed(self, new_dirs: list[str]):
        self._cfg.directories = new_dirs
        save_app_config(self._cfg)
        self._populate_thumbnails()

    def _on_about(self, _btn):
        about = Gtk.AboutDialog(
            transient_for=self,
            modal=True,
            program_name="nitrogen-gtk4",
            version=__version__,
            comments="Modern GTK4 background manager for X11",
            license_type=Gtk.License.GPL_2_0,
        )
        about.present()

    def _show_message(self, text: str):
        dlg = Gtk.AlertDialog()
        dlg.set_message(text)
        dlg.show(self)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

class NitrogenApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=__app_id__)

    def do_activate(self):
        win = MainWindow(self)
        win.present()


def run_gui() -> int:
    app = NitrogenApp()
    return app.run(None)

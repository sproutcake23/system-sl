"""Built-in palettes and wallpaper path helpers.

Colour extraction from the wallpaper now lives in
:mod:`system_sl.utils.wallust_theme`. This module keeps the built-in dark
palette, the image-extension list, and the path/decoding helpers shared by the
wallust provider.
"""

import os
import re

from PIL import Image

# The default (System dark) palette: THE SYSTEM's Solo Leveling navy.
DEFAULT_TOKENS = {
    "sys_color_background": "#060912",
    "sys_color_surface": "#0b0f1a",
    "sys_color_surface_raised": "#0e1626",
    "sys_color_border": "#1f6f99",
    "sys_color_border_muted": "#1a2638",
    "sys_color_primary": "#00d9ff",
    "sys_color_primary_light": "#67e8ff",
    "sys_color_primary_dim": "rgba(0, 217, 255, 0.08)",
    "sys_color_primary_hover": "rgba(0, 217, 255, 0.18)",
    "sys_color_primary_pressed": "rgba(0, 217, 255, 0.30)",
    "sys_color_text_main": "#d6e7ff",
    "sys_color_text_secondary": "#b3d4ff",
    "sys_color_text_highlight": "#b6f1ff",
    "sys_color_text_muted": "#3d4d68",
    "sys_color_on_primary": "#04121a",
    "sys_color_button_text": "#67e8ff",
    "sys_color_success": "#4ade80",
    "sys_color_alert": "#ff6b8a",
    "sys_color_on_alert": "#ffffff",
    "sys_color_disabled_surface": "rgba(255, 255, 255, 0.02)",
    "sys_color_selection_text": "#ffffff",
}

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
              ".tif", ".tiff", ".avif", ".jxl")

# Formats wallust's bundled image decoder may not read. They are converted to
# PNG with Pillow/imagecodecs before extraction.
PRECONVERT_EXTS = (".jxl", ".avif", ".heic", ".heif")


def rgb_to_hex(rgb_tuple):
    r, g, b = [int(x * 255) for x in rgb_tuple]
    return f"#{r:02x}{g:02x}{b:02x}"


def _pick_largest(files):
    """Return the highest-resolution file based on the WxH in its name."""
    best, best_area = None, -1
    for f in files:
        area = 0
        m = re.search(r"(\d+)[xX](\d+)", os.path.splitext(os.path.basename(f))[0])
        if m:
            area = int(m.group(1)) * int(m.group(2))
        if area > best_area:
            best, best_area = f, area
    return best if best is not None else (files[0] if files else None)


def resolve_wallpaper_path(path):
    """Turn a wallpaper path (file or KDE collection directory) into an image file."""
    if not path:
        return None
    path = os.path.expanduser(str(path))
    if os.path.isfile(path):
        return path
    if not os.path.isdir(path):
        return None
    # KDE wallpaper package layout: contents/images or contents/images_dark
    for sub in ("contents/images_dark", "contents/images"):
        d = os.path.join(path, sub)
        if os.path.isdir(d):
            candidates = [os.path.join(d, n) for n in sorted(os.listdir(d))
                          if n.lower().endswith(IMAGE_EXTS)]
            if candidates:
                return _pick_largest(candidates)
    candidates = []
    for root, _, files in os.walk(path):
        for n in sorted(files):
            if n.lower().endswith(IMAGE_EXTS):
                candidates.append(os.path.join(root, n))
    return _pick_largest(candidates) if candidates else None


def open_image(path):
    """Open an image, decoding JPEG XL (Fedora stock wallpapers) via imagecodecs."""
    try:
        return Image.open(path)
    except Exception:
        if path.lower().endswith(".jxl"):
            from imagecodecs import jpegxl_decode
            with open(path, "rb") as f:
                arr = jpegxl_decode(f.read())
            if arr.ndim == 3 and arr.shape[2] == 4:
                return Image.fromarray(arr, "RGBA")
            if arr.ndim == 3 and arr.shape[2] == 3:
                return Image.fromarray(arr, "RGB")
            return Image.fromarray(arr)
        raise


# Kept for callers that still import the old private name.
_open_image = open_image

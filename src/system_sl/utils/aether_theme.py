"""Read Aether's applied color palette and map it to SL's theme tokens.

Aether writes the currently-applied theme colors to::

    ~/.local/state/omarchy/current/theme/colors.toml

This module parses that file (produced when you run ``aether --generate <wallpaper>``
or when Omarchy applies a theme) and converts the semantic color names into the
``sys_color_*`` tokens used by :mod:`system_sl.frontend.gui.theme`.

Because Aether already extracts the palette from the wallpaper, SL does not need
to re-extract colors. It simply watches this file for changes and re-applies the
stylesheet when Aether applies a new theme.
"""

from __future__ import annotations

import colorsys
import os
import tomllib
from typing import Dict, Optional

# Fallback tokens identical to theme_gen.DEFAULT_TOKENS so SL keeps its
# Solo Leveling look when Aether has not yet applied a theme.
DEFAULT_TOKENS: Dict[str, str] = {
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
}

# Default path to Aether's live theme file (Omarchy-managed).
AETHER_COLORS_TOML = os.path.expanduser(
    "~/.local/state/omarchy/current/theme/colors.toml"
)

# Fallback locations in case the user stores the theme elsewhere.
AETHER_COLORS_FALLBACKS = [
    os.path.expanduser("~/.config/aether/theme/colors.toml"),
    os.path.expanduser("~/.config/omarchy/themes/aether/colors.toml"),
]


def _hex_to_rgb(hex_color: str) -> Optional[tuple[float, float, float]]:
    """Convert ``#RRGGBB`` to normalized (r, g, b) in [0, 1]."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        return None
    try:
        r = int(hex_color[0:2], 16) / 255.0
        g = int(hex_color[2:4], 16) / 255.0
        b = int(hex_color[4:6], 16) / 255.0
    except ValueError:
        return None
    return r, g, b


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    """Convert normalized (r, g, b) to ``#RRGGBB``."""
    r, g, b = [int(max(0, min(255, x * 255))) for x in rgb]
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgba(hex_color: str, alpha: float) -> str:
    """Return ``rgba(r, g, b, a)`` for a hex color."""
    rgb = _hex_to_rgb(hex_color)
    if rgb is None:
        return f"rgba(0, 0, 0, {alpha})"
    r_int, g_int, b_int = [int(x * 255) for x in rgb]
    return f"rgba({r_int}, {g_int}, {b_int}, {alpha})"


def _lighten(hex_color: str, amount: float = 0.25) -> str:
    """Lighten a hex color by *amount* (0-1) in HSL space."""
    rgb = _hex_to_rgb(hex_color)
    if rgb is None:
        return hex_color
    h, l, s = colorsys.rgb_to_hls(*rgb)
    l = min(1.0, l + amount * (1.0 - l))
    return _rgb_to_hex(colorsys.hls_to_rgb(h, l, s))


def _darken(hex_color: str, amount: float = 0.25) -> str:
    """Darken a hex color by *amount* (0-1) in HSL space."""
    rgb = _hex_to_rgb(hex_color)
    if rgb is None:
        return hex_color
    h, l, s = colorsys.rgb_to_hls(*rgb)
    l = max(0.0, l - amount * l)
    return _rgb_to_hex(colorsys.hls_to_rgb(h, l, s))


def find_aether_colors_toml() -> Optional[str]:
    """Return the path to Aether's current theme file, or ``None`` if missing."""
    if os.path.isfile(AETHER_COLORS_TOML):
        return AETHER_COLORS_TOML
    for fallback in AETHER_COLORS_FALLBACKS:
        if os.path.isfile(fallback):
            return fallback
    return None


def load_aether_palette(path: Optional[str] = None) -> Optional[Dict[str, str]]:
    """Parse Aether's ``colors.toml`` into a flat ``{name: hex}`` dict.

    Returns ``None`` if the file cannot be found or parsed.
    """
    path = path or find_aether_colors_toml()
    if not path:
        return None

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None

    if not isinstance(data, dict):
        return None

    # Only keep scalar string color values (skip "mode", nested tables, etc.).
    palette: Dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, str) and value.startswith("#"):
            palette[key] = value
    return palette or None


def generate_aether_tokens(path: Optional[str] = None) -> Dict[str, str]:
    """Build SL ``sys_color_*`` tokens from Aether's palette.

    Falls back to :data:`DEFAULT_TOKENS` when Aether's theme file is unavailable,
    so SL always has a usable theme.
    """
    palette = load_aether_palette(path)
    if not palette:
        return dict(DEFAULT_TOKENS)

    # Direct mappings from Aether semantic names to SL token names.
    background = palette.get("background", DEFAULT_TOKENS["sys_color_background"])
    lighter_bg = palette.get(
        "lighter_background", DEFAULT_TOKENS["sys_color_surface"]
    )
    selection = palette.get(
        "selection", DEFAULT_TOKENS["sys_color_surface_raised"]
    )
    accent = palette.get("accent", DEFAULT_TOKENS["sys_color_primary"])
    foreground = palette.get(
        "foreground", DEFAULT_TOKENS["sys_color_text_main"]
    )
    muted = palette.get("muted", DEFAULT_TOKENS["sys_color_text_muted"])
    blue = palette.get("blue", DEFAULT_TOKENS["sys_color_border"])

    # Derived tokens that Aether does not name explicitly.
    primary_light = _lighten(accent, 0.45)
    border_muted = _darken(muted, 0.20) if muted != DEFAULT_TOKENS["sys_color_text_muted"] else DEFAULT_TOKENS["sys_color_border_muted"]
    text_secondary = palette.get(
        "light_foreground", _lighten(foreground, 0.25)
    )
    text_highlight = palette.get(
        "bright_foreground", _lighten(foreground, 0.45)
    )

    r_int, g_int, b_int = [int(x * 255) for x in (_hex_to_rgb(accent) or (0, 217, 255))]

    return {
        "sys_color_background": background,
        "sys_color_surface": lighter_bg,
        "sys_color_surface_raised": selection,
        "sys_color_border": blue,
        "sys_color_border_muted": border_muted,
        "sys_color_primary": accent,
        "sys_color_primary_light": primary_light,
        "sys_color_primary_dim": f"rgba({r_int}, {g_int}, {b_int}, 0.08)",
        "sys_color_primary_hover": f"rgba({r_int}, {g_int}, {b_int}, 0.18)",
        "sys_color_primary_pressed": f"rgba({r_int}, {g_int}, {b_int}, 0.30)",
        "sys_color_text_main": foreground,
        "sys_color_text_secondary": text_secondary,
        "sys_color_text_highlight": text_highlight,
        "sys_color_text_muted": muted,
    }

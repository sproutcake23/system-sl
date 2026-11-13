"""Find the active wallpaper and describe it in a way that can be cached.

A wallpaper change is detected from a *fingerprint* (resolved path, size and
mtime), not from the path alone. That catches a slideshow replacing a file in
place, a KDE wallpaper package being swapped, and Windows rewriting its
transcoded wallpaper.

``get_wallpaper_state()`` returns a :class:`WallpaperState`; the older
``get_wallpaper_path()`` string API is kept for callers that only need a path.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Optional

from system_sl.utils.theme_gen import IMAGE_EXTS, resolve_wallpaper_path

OMARCHY_CURRENT = os.path.expanduser("~/.local/state/omarchy/current")
OMARCHY_BACKGROUND = os.path.join(OMARCHY_CURRENT, "background")


@dataclass(frozen=True)
class WallpaperState:
    """The active wallpaper and a fingerprint that changes with its content."""

    source: str
    image_path: Optional[str] = None
    fingerprint: str = ""
    extra: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return bool(self.image_path and self.fingerprint)


def _fingerprint(path: str, extra: str = "") -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    resolved = os.path.realpath(path)
    raw = f"{resolved}|{stat.st_mtime_ns}|{stat.st_size}|{extra}"
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def _state(source: str, candidate: Optional[str], extra: str = "", error: Optional[str] = None) -> WallpaperState:
    resolved = resolve_wallpaper_path(candidate) if candidate else None
    if resolved:
        return WallpaperState(
            source=source,
            image_path=resolved,
            fingerprint=_fingerprint(resolved, extra),
            extra=extra,
        )
    return WallpaperState(
        source=source,
        error=error or (
            f"No wallpaper image found for {source}."
            if not candidate
            else f"The wallpaper {candidate} could not be opened."
        ),
    )


def state_for_image(path: str, source: str = "manual") -> WallpaperState:
    """Describe a specific image file as the wallpaper (a manual choice)."""
    return _state(source, path)


def _first_existing_line(output: Optional[str]) -> Optional[str]:
    if not output:
        return None

    def clean(part: str) -> Optional[str]:
        part = part.strip().strip("'\"")
        if not part:
            return None
        expanded = os.path.expanduser(part)
        if os.path.isfile(expanded):
            return expanded
        # Tools append metadata after the path (" (scale 1.0)") or list entries
        # monitor-first ("eDP-1: image: /path"). Try the obvious cut points.
        for separator in (" (", ", "):
            if separator in part:
                trimmed = os.path.expanduser(part.split(separator, 1)[0].strip())
                if os.path.isfile(trimmed):
                    return trimmed
        if part.lower().endswith(IMAGE_EXTS):
            return expanded
        return None

    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        candidates = []
        if "image:" in line:
            candidates.append(line.split("image:", 1)[1])
        if "=" in line:
            candidates.append(line.split("=", 1)[1])
        # hyprpaper/swww prefix each line with the monitor: "eDP-1: /path".
        if ":" in line:
            candidates.append(line.split(":", 1)[-1])
        if "=" not in line and ":" not in line:
            candidates.append(line)
        for part in candidates:
            candidate = clean(part)
            if candidate:
                return candidate
    return None


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------

def _windows_state() -> WallpaperState:
    extra = ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
            path, _ = winreg.QueryValueEx(key, "Wallpaper")
    except Exception as exc:  # FileNotFoundError, OSError, ImportError
        path = None
        error = f"Error reading wallpaper registry: {exc}"
    else:
        error = None

    # Slideshows update TranscodedWallpaper rather than the registry string, so
    # its timestamp belongs in the fingerprint.
    transcoded = os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~\\AppData\\Roaming")),
        "Microsoft", "Windows", "Themes", "TranscodedWallpaper",
    )
    if os.path.isfile(transcoded):
        try:
            extra = f"transcoded:{os.stat(transcoded).st_mtime_ns}"
        except OSError:
            pass
        if not path or not os.path.isfile(os.path.expanduser(path)):
            path = transcoded

    state = _state("windows", path, extra=extra, error=error)
    if state.ok:
        return state
    if os.path.isfile(transcoded):
        return _state("windows-transcoded", transcoded, extra=extra)
    return state


# --------------------------------------------------------------------------
# macOS
# --------------------------------------------------------------------------

def _macos_state() -> WallpaperState:
    script = 'tell application "System Events" to tell every desktop to get picture'
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception as exc:
        return WallpaperState(source="macos", error=f"Error running osascript: {exc}")

    # One path per desktop; the first desktop is the primary one.
    candidates = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not candidates:
        return WallpaperState(source="macos", error="No macOS wallpaper found.")

    extra = ""
    index = os.path.expanduser(
        "~/Library/Application Support/com.apple.wallpaper/Store/Index.plist"
    )
    if os.path.isfile(index):
        try:
            extra = f"index:{os.stat(index).st_mtime_ns}"
        except OSError:
            pass

    for candidate in candidates:
        state = _state("macos", candidate, extra=extra)
        if state.ok:
            return state
    return WallpaperState(source="macos", error=candidates[0])


# --------------------------------------------------------------------------
# Linux desktop environments
# --------------------------------------------------------------------------

def _gnome_state() -> WallpaperState:
    try:
        theme_req = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        uri_key = "picture-uri-dark" if "prefer-dark" in theme_req.stdout else "picture-uri"
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.background", uri_key],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except FileNotFoundError:
        return WallpaperState(source="gnome", error="Error: gsettings not found.")
    except subprocess.CalledProcessError as exc:
        return WallpaperState(source="gnome", error=f"Error running gsettings: {exc}")
    except subprocess.SubprocessError as exc:
        return WallpaperState(source="gnome", error=f"Error running gsettings: {exc}")

    path = result.stdout.strip().strip("'\"")
    path = path.replace("file://", "")
    if not path:
        return WallpaperState(source="gnome", error="No active GNOME wallpaper found.")

    if path.lower().endswith(".xml"):
        # A slideshow XML lists its images; use the first one. The XML's mtime
        # is part of the fingerprint so editing the slideshow is noticed too.
        extra = _gnome_slideshow_extra(path)
        for image in _gnome_slideshow_images(path):
            state = _state("gnome-slideshow", image, extra=extra)
            if state.ok:
                return state
        return WallpaperState(source="gnome-slideshow", error=path)

    return _state("gnome", path)


def _gnome_slideshow_images(path: str) -> list[str]:
    try:
        tree = ET.parse(path)
    except (OSError, ET.ParseError):
        return []
    images = []
    for element in tree.getroot().iter():
        if element.tag.endswith("file") and element.text:
            images.append(element.text.strip().replace("file://", ""))
    return images


def _gnome_slideshow_extra(path: str) -> str:
    try:
        return f"xml:{os.stat(path).st_mtime_ns}"
    except OSError:
        return ""


def _kde_candidate() -> tuple[Optional[str], Optional[str]]:
    config_path = os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc")
    if not os.path.exists(config_path):
        return None, "Error: KDE config file not found."

    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()

        containments = {}
        current = None
        section = None
        for line in lines:
            if line.startswith("[Containments][") and line.endswith("]"):
                parts = line[1:-1].split("][")
                if len(parts) == 2 and parts[0] == "Containments":
                    current = parts[1]
                    containments.setdefault(current, {"images": {}})
                    section = ("containment",)
                    continue
            if current is None:
                continue
            if line == f"[Containments][{current}][General]":
                section = ("general",)
                continue
            if (line.startswith(f"[Containments][{current}][Wallpaper][")
                    and line.endswith("][General]")):
                plugin = line[len(f"[Containments][{current}][Wallpaper]["):-len("][General]")]
                section = ("wallpaper", plugin)
                continue
            if line.startswith("["):
                section = None
                continue
            if section is None:
                continue
            if section[0] == "containment":
                if line.startswith("formfactor="):
                    containments[current]["formfactor"] = line.split("=", 1)[1].strip()
                elif line.startswith("wallpaperplugin="):
                    containments[current]["wallpaperplugin"] = line.split("=", 1)[1].strip()
            elif section[0] == "wallpaper" and line.startswith("Image="):
                img = line.split("=", 1)[1].strip().replace("file://", "")
                containments[current]["images"][section[1]] = img

        desktops = [c for c in containments.values() if c.get("formfactor") == "0"]
        if not desktops:
            desktops = list(containments.values())

        candidates = []
        for containment in desktops:
            plugin = containment.get("wallpaperplugin")
            if plugin and plugin in containment.get("images", {}):
                candidates.append(containment["images"][plugin])
        for containment in desktops:
            if "org.kde.image" in containment.get("images", {}):
                candidates.append(containment["images"]["org.kde.image"])
        for containment in containments.values():
            candidates.extend(containment.get("images", {}).values())

        for candidate in candidates:
            if resolve_wallpaper_path(candidate):
                return candidate, None
        if candidates:
            return candidates[0], None
    except Exception as exc:
        return None, f"Error reading KDE config: {exc}"
    return None, "No active KDE wallpaper found in config."


def _cosmic_candidate() -> tuple[Optional[str], Optional[str]]:
    config_path = os.path.expanduser("~/.config/cosmic/com.system76.CosmicBackground/v1/all")
    if not os.path.exists(config_path):
        return None, "Error: COSMIC config file not found."
    try:
        with open(config_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        return None, f"Error reading COSMIC config: {exc}"

    match = re.search(r'Path\(\s*"([^"]+)"\s*\)', text)
    if match:
        return match.group(1), None
    # Older COSMIC builds wrote a plain "image_path: ..." line.
    for line in text.splitlines():
        if "source:" in line and '"' in line:
            parts = line.split('"')
            if len(parts) >= 2:
                return parts[1], None
    return None, "No active COSMIC wallpaper found in config."


def _hyprland_candidate() -> tuple[Optional[str], Optional[str]]:
    # Omarchy renders the background through Quickshell, so no wallpaper daemon
    # is running and the process scan below finds nothing. Its symlink is the
    # authoritative current wallpaper.
    if os.path.exists(OMARCHY_BACKGROUND):
        return OMARCHY_BACKGROUND, None

    # hyprpaper: `hyprctl hyprpaper listactive` prints one entry per monitor.
    try:
        result = subprocess.run(
            ["hyprctl", "hyprpaper", "listactive"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        candidate = _first_existing_line(result.stdout)
        if candidate:
            return candidate, None
    except (OSError, subprocess.SubprocessError):
        pass

    # Command-line setters (swaybg, wbg).
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as handle:
                    cmd = [part.decode("utf-8", "ignore") for part in handle.read().split(b"\x00") if part]
            except (OSError, PermissionError):
                continue
            if not cmd:
                continue
            if "swaybg" in cmd[0] or "swaybg" in cmd:
                if "-i" in cmd:
                    return cmd[cmd.index("-i") + 1], None
            if "wbg" in cmd[0] or "wbg" in cmd:
                if len(cmd) > 1:
                    return cmd[1], None
    except OSError:
        pass

    hyprpaper_config = os.path.expanduser("~/.config/hypr/hyprpaper.conf")
    if os.path.exists(hyprpaper_config):
        try:
            with open(hyprpaper_config, "r", encoding="utf-8", errors="replace") as handle:
                for line in reversed(handle.readlines()):
                    line = line.strip()
                    if line.startswith("wallpaper"):
                        parts = line.split(",")
                        if len(parts) > 1:
                            return parts[1].strip(), None
        except OSError:
            pass

    # swww prints "<monitor>: image: <path>" per monitor.
    try:
        result = subprocess.run(
            ["swww", "query"],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        candidate = _first_existing_line(result.stdout)
        if candidate:
            return candidate, None
    except (OSError, subprocess.SubprocessError):
        pass

    return None, "No known Hyprland wallpaper daemon is running."


def _xfce_candidate() -> tuple[Optional[str], Optional[str]]:
    config_path = os.path.expanduser(
        "~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml"
    )
    if not os.path.exists(config_path):
        return None, "Error: XFCE desktop XML not found."
    try:
        tree = ET.parse(config_path)
        for prop in tree.getroot().iter("property"):
            if prop.get("name") == "last-image":
                return prop.get("value", ""), None
    except ET.ParseError:
        return None, "Error parsing XFCE XML file."
    except OSError as exc:
        return None, f"Error reading XFCE config: {exc}"
    return None, "No XFCE wallpaper found."


def _linux_state() -> WallpaperState:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()

    # Omarchy is the most specific case, and it works even when the wallpaper
    # is drawn by Quickshell rather than a wallpaper daemon.
    if os.path.exists(OMARCHY_BACKGROUND):
        state = _state("omarchy", OMARCHY_BACKGROUND)
        if state.ok:
            return state

    candidate = None
    error = None
    source = desktop or "linux"

    if "gnome" in desktop or "unity" in desktop or "pantheon" in desktop:
        return _gnome_state()
    if "kde" in desktop or "plasma" in desktop:
        source = "kde"
        candidate, error = _kde_candidate()
    elif "cosmic" in desktop:
        source = "cosmic"
        candidate, error = _cosmic_candidate()
    elif "hyprland" in desktop or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        source = "hyprland"
        candidate, error = _hyprland_candidate()
    elif "xfce" in desktop:
        source = "xfce"
        candidate, error = _xfce_candidate()
    else:
        # Unknown desktop: try the common backends in order of specificity.
        for name, probe in (
            ("kde", _kde_candidate),
            ("cosmic", _cosmic_candidate),
            ("gnome", None),
            ("hyprland", _hyprland_candidate),
            ("xfce", _xfce_candidate),
        ):
            source = name
            if probe is None:
                state = _gnome_state()
                if state.ok:
                    return state
                error = state.error
                candidate = None
                continue
            candidate, error = probe()
            if candidate:
                break

    if candidate:
        state = _state(source, candidate, error=error)
        if state.ok:
            return state
    if error:
        return WallpaperState(source=source, error=error)

    # A last-resort GNOME probe helps on desktops that do not set
    # XDG_CURRENT_DESKTOP but still use gsettings.
    gnome = _gnome_state()
    if gnome.ok:
        return gnome
    return WallpaperState(source=source or "linux", error=error or "No wallpaper found.")


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def get_wallpaper_state() -> WallpaperState:
    """Return the active wallpaper plus a change-detection fingerprint."""
    system_name = platform.system().lower()
    if system_name == "windows":
        return _windows_state()
    if system_name == "darwin":
        return _macos_state()
    if system_name == "linux":
        return _linux_state()
    return WallpaperState(source=system_name or "unknown", error=f"Unsupported OS: {system_name}")


def get_wallpaper_path() -> str:
    """Return the active wallpaper path, or a human-readable error string."""
    state = get_wallpaper_state()
    if state.image_path:
        return state.image_path
    return state.error or "No wallpaper detected."


def watch_paths() -> list[str]:
    """Files and directories whose change may mean the wallpaper changed.

    Only existing paths are returned: ``QFileSystemWatcher`` logs a warning for
    paths it cannot watch.
    """
    candidates = [
        OMARCHY_BACKGROUND,
        OMARCHY_CURRENT,
        os.path.expanduser("~/.config/hypr/hyprpaper.conf"),
        os.path.expanduser("~/.config/kdeglobals"),
        os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc"),
        os.path.expanduser("~/.config/cosmic/com.system76.CosmicBackground/v1/all"),
        os.path.expanduser(
            "~/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml"
        ),
        os.path.expanduser("~/.config/gtk-3.0/settings.ini"),
        os.path.expanduser("~/.config/gtk-4.0/settings.ini"),
    ]

    system_name = platform.system().lower()
    if system_name == "windows":
        appdata = os.environ.get("APPDATA", "")
        themes = os.path.join(appdata, "Microsoft", "Windows", "Themes")
        candidates += [themes, os.path.join(themes, "TranscodedWallpaper")]
    elif system_name == "darwin":
        store = os.path.expanduser(
            "~/Library/Application Support/com.apple.wallpaper/Store"
        )
        candidates += [store, os.path.join(store, "Index.plist")]

    seen = set()
    paths = []
    for path in candidates:
        if not path or path in seen:
            continue
        seen.add(path)
        if os.path.exists(path):
            paths.append(path)
    return paths

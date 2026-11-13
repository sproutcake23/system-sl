"""Generate THE SYSTEM's theme tokens from the wallpaper with wallust.

wallust is an external MIT-licensed binary. It is bundled with the released
app, and ``find_wallust()`` also accepts a system installation or an explicit
``SYSTEM_SL_WALLUST`` path.

Each extraction runs in a private temporary config directory, so the user's
``~/.config/wallust`` is never read or modified, no hooks run, and no terminal
escape sequences are emitted. Results are cached on disk (and in memory) by a
fingerprint of the image, so startup and chat rendering do not re-run wallust
unless the wallpaper actually changed.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

from system_sl.utils.theme_gen import (
    DEFAULT_TOKENS,
    PRECONVERT_EXTS,
    open_image,
    resolve_wallpaper_path,
)

LOG = logging.getLogger(__name__)

WALLUST_CONFIG_VERSION = "1"
# Bump whenever palette_to_tokens() changes how colours are derived, so cached
# palettes made by the old mapping are not served after an upgrade.
PALETTE_MAPPING_VERSION = "2"
WALLUST_BIN_ENV = "SYSTEM_SL_WALLUST"
WALLUST_TIMEOUT = 25.0

# Running wallust processes, so a shutdown can end them. Without this a worker
# thread blocked in communicate() can outlive the 5 s shutdown wait (the
# timeout is 25 s), and Qt aborts when it destroys a thread that is still
# running.
_process_lock = threading.Lock()
_active_processes: "set[subprocess.Popen]" = set()
_cancelled = threading.Event()

PALETTE_KEYS = (
    "background",
    "foreground",
    "cursor",
    *(f"color{i}" for i in range(16)),
)

_MISSING_WALLUST_MESSAGE = (
    "Dynamic themes need wallust, but it was not found. "
    "Install wallust or set SYSTEM_SL_WALLUST; the built-in palette is being used."
)


_memory_cache: Optional[dict] = None
_warned_unavailable = False
_warned_failure = False


# --------------------------------------------------------------------------
# Binary and cache locations
# --------------------------------------------------------------------------

def _wallust_names() -> tuple[str, ...]:
    return ("wallust.exe", "wallust") if os.name == "nt" else ("wallust",)


def _bundled_dirs():
    """Directories that may contain a bundled wallust binary."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        yield Path(meipass)
        yield Path(meipass) / "_internal"
    executable = Path(sys.executable)
    yield executable.parent
    yield executable.parent / "_internal"
    # A source checkout built locally: <repo>/vendor/wallust/wallust.
    yield Path(__file__).resolve().parents[3] / "vendor" / "wallust"


def find_wallust() -> Optional[str]:
    """Return the wallust executable to use, or ``None`` when unavailable."""
    override = os.environ.get(WALLUST_BIN_ENV)
    if override:
        candidate = Path(os.path.expanduser(override))
        if candidate.is_file():
            return str(candidate)

    for directory in _bundled_dirs():
        for name in _wallust_names():
            candidate = directory / name
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)

    return shutil.which("wallust")


def wallust_available() -> bool:
    return find_wallust() is not None


def missing_wallust_message() -> str:
    return _MISSING_WALLUST_MESSAGE


def cache_dir() -> Path:
    override = os.environ.get("SYSTEM_SL_CACHE_DIR")
    if override:
        return Path(os.path.expanduser(override)) / "system-sl"
    system = platform.system().lower()
    if system == "windows":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    elif system == "darwin":
        base = os.path.expanduser("~/Library/Caches")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "system-sl"


def _cache_file() -> Path:
    return cache_dir() / "wallust-tokens.json"


def _ensure_cache_dir() -> Path:
    directory = cache_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _wallust_identity() -> str:
    """Identify the wallust binary in use by path, size and mtime.

    A different or upgraded wallust can extract different colours from the
    same image, so it invalidates the cache. Stat only: no subprocess.
    """
    binary = find_wallust()
    if not binary:
        return "none"
    try:
        stat = os.stat(binary)
    except OSError:
        return binary
    return f"{os.path.realpath(binary)}:{stat.st_size}:{stat.st_mtime_ns}"


def _cache_version() -> str:
    return f"{WALLUST_CONFIG_VERSION}.{PALETTE_MAPPING_VERSION}"


def _file_fingerprint(path: str) -> str:
    try:
        stat = os.stat(path)
    except OSError:
        return ""
    raw = (
        f"{os.path.realpath(path)}|{stat.st_mtime_ns}|{stat.st_size}"
        f"|wallust{_cache_version()}|{_wallust_identity()}"
    )
    return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


def _valid_tokens(tokens: object) -> bool:
    if not isinstance(tokens, dict):
        return False
    return all(isinstance(tokens.get(key), str) and tokens[key] for key in DEFAULT_TOKENS)


def _read_cache() -> Optional[dict]:
    try:
        with open(_cache_file(), "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("version") != _cache_version():
        return None
    if not _valid_tokens(payload.get("tokens")):
        return None
    return payload


def _write_cache(fingerprint: str, image_path: str, tokens: Dict[str, str]) -> None:
    global _memory_cache
    payload = {
        "version": _cache_version(),
        "fingerprint": fingerprint,
        "image": image_path,
        "tokens": tokens,
    }
    _memory_cache = payload
    handle = None
    try:
        directory = _ensure_cache_dir()
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=directory,
            prefix=".wallust-tokens-",
            suffix=".tmp",
            delete=False,
        )
        try:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()
        os.replace(handle.name, _cache_file())
        handle = None
    except (OSError, TypeError, ValueError) as exc:
        LOG.debug("Could not write the wallust token cache: %s", exc)
    finally:
        # Anything that stopped before the rename leaves its temp file behind.
        if handle is not None:
            try:
                os.unlink(handle.name)
            except OSError:
                pass


def _cached_payload() -> Optional[dict]:
    global _memory_cache
    if _memory_cache is None:
        _memory_cache = _read_cache()
    if _memory_cache and _valid_tokens(_memory_cache.get("tokens")):
        return _memory_cache
    return None


def cached_wallust_tokens_or_none() -> Optional[Dict[str, str]]:
    """Return the last generated tokens, or None when nothing is cached."""
    payload = _cached_payload()
    return dict(payload["tokens"]) if payload else None


def cached_wallust_tokens() -> Dict[str, str]:
    """Return the last generated tokens without running wallust.

    Startup uses this to paint immediately; the real extraction then refreshes
    it in the background. If there is no cache, the built-in dark tokens are a
    safe, readable theme.
    """
    return cached_wallust_tokens_or_none() or dict(DEFAULT_TOKENS)


def cached_tokens_for(image_path: str) -> Optional[Dict[str, str]]:
    """Return the cached palette if it was made from this exact image.

    Only stats the file, so the GUI thread can ask before deciding whether a
    wallust run is needed at all.
    """
    payload = _cached_payload()
    if not payload:
        return None
    resolved = resolve_wallpaper_path(image_path)
    if not resolved:
        return None
    if payload.get("fingerprint") != _file_fingerprint(resolved):
        return None
    return dict(payload["tokens"])


# --------------------------------------------------------------------------
# wallust invocation
# --------------------------------------------------------------------------

_PALETTE_TEMPLATE = "{\n" + ",\n".join(
    f'  "{key}": "{{{{{key}}}}}"' for key in PALETTE_KEYS
) + "\n}\n"


def _write_private_config(config_dir: Path, target: Path) -> None:
    templates = config_dir / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    (templates / "palette.json").write_text(_PALETTE_TEMPLATE, encoding="utf-8")

    # Forward slashes are valid TOML on every platform and avoid backslash
    # escaping in Windows paths.
    config = (
        'backend = "fastresize"\n'
        'color_space = "salience"\n'
        'palette = "saliencedark"\n'
        "check_contrast = true\n"
        "\n[templates]\n"
        # json.dumps gives a valid TOML basic string, so a path holding a
        # quote or a backslash can't end the string early.
        f"system_sl = {{ template = 'palette.json', target = {json.dumps(target.as_posix())} }}\n"
    )
    (config_dir / "wallust.toml").write_text(config, encoding="utf-8")


def _convert_to_png(image_path: str, directory: Path) -> Optional[str]:
    """Convert a format wallust may not decode (JXL/AVIF/HEIC) to PNG."""
    target = directory / "wallpaper.png"
    try:
        with open_image(image_path) as image:
            image.convert("RGB").save(target, format="PNG")
        return str(target)
    except Exception as exc:
        LOG.debug("Could not convert %s for wallust: %s", image_path, exc)
        return None


def _run_wallust_once(binary: str, image_path: str) -> Optional[Dict[str, str]]:
    directory = Path(tempfile.mkdtemp(prefix="wallust-", dir=str(_ensure_cache_dir())))
    try:
        config_dir = directory / "config"
        config_dir.mkdir()
        target = directory / "palette.json"
        _write_private_config(config_dir, target)

        command = [
            binary,
            "run",
            image_path,
            "-d",
            str(config_dir),
            "-s",
            "--no-hooks",
            "-q",
            "-n",
        ]
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        # Popen rather than run(), so cancel_extractions() can end the child
        # process instead of the thread that is waiting on it having to be
        # killed. The start and the registration share the lock, so a cancel
        # cannot slip in between them and miss the new process.
        with _process_lock:
            if _cancelled.is_set():
                return None
            try:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=creationflags,
                    # Its own process group on POSIX, so a kill also reaches
                    # any child it spawned. A child that survives would keep
                    # the pipes open and communicate() blocked.
                    start_new_session=os.name != "nt",
                )
            except OSError as exc:
                LOG.debug("wallust failed to start: %s", exc)
                return None
            _active_processes.add(process)

        try:
            stdout, stderr = process.communicate(timeout=WALLUST_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_process(process)
            process.communicate()
            LOG.debug("wallust timed out after %ss", WALLUST_TIMEOUT)
            return None
        finally:
            with _process_lock:
                _active_processes.discard(process)

        if _cancelled.is_set():
            return None

        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        if result.returncode != 0 or not target.is_file():
            LOG.debug(
                "wallust exited %s: %s",
                result.returncode,
                (result.stderr or result.stdout or "").strip()[:500],
            )
            return None

        try:
            with open(target, "r", encoding="utf-8") as handle:
                palette = json.load(handle)
        except (OSError, ValueError) as exc:
            LOG.debug("wallust produced unreadable JSON: %s", exc)
            return None
        return _validate_palette(palette)
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _validate_palette(palette: object) -> Optional[Dict[str, str]]:
    if not isinstance(palette, dict):
        return None
    cleaned: Dict[str, str] = {}
    for key in PALETTE_KEYS:
        value = palette.get(key)
        if not isinstance(value, str) or not _parse_hex(value):
            return None
        cleaned[key] = _normalise_hex(value)
    return cleaned


def cancel_extractions() -> None:
    """Kill any running wallust process and refuse to start new ones.

    Called once at shutdown. It is final: extraction stays disabled for the
    rest of the process, so a worker cannot start its next retry after its
    current wallust run is killed.
    """
    with _process_lock:
        _cancelled.set()
        processes = list(_active_processes)
    for process in processes:
        _kill_process(process)


def _kill_process(process: subprocess.Popen) -> None:
    """Kill wallust and, on POSIX, every process in its group."""
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except OSError:
        pass  # It already exited.


def _extract_palette(image_path: str) -> Optional[Dict[str, str]]:
    binary = find_wallust()
    if not binary:
        global _warned_unavailable
        if not _warned_unavailable:
            _warned_unavailable = True
            LOG.warning(_MISSING_WALLUST_MESSAGE)
        return None

    # wallust's image decoder may not handle JXL/AVIF/HEIC. Convert those first.
    if image_path.lower().endswith(PRECONVERT_EXTS):
        directory = Path(tempfile.mkdtemp(prefix="wallust-convert-", dir=str(_ensure_cache_dir())))
        try:
            converted = _convert_to_png(image_path, directory)
            if converted:
                palette = _run_wallust_once(binary, converted)
                if palette:
                    return palette
        finally:
            shutil.rmtree(directory, ignore_errors=True)

    palette = _run_wallust_once(binary, image_path)
    if palette:
        return palette

    # Some other format failed to decode: let Pillow try once as PNG, unless the
    # extension already said it needed conversion.
    if not image_path.lower().endswith(PRECONVERT_EXTS):
        directory = Path(tempfile.mkdtemp(prefix="wallust-convert-", dir=str(_ensure_cache_dir())))
        try:
            converted = _convert_to_png(image_path, directory)
            if converted:
                return _run_wallust_once(binary, converted)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
    return None


@dataclass(frozen=True)
class ExtractionResult:
    """What one palette extraction produced.

    ``tokens`` is None when it failed; ``error`` then says why, in words a
    player can act on. ``cancelled`` marks a run stopped by shutdown, which
    is not a failure worth reporting.
    """

    tokens: Optional[Dict[str, str]] = None
    error: Optional[str] = None
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.tokens is not None


def extract_wallust_tokens(image_path: str, *, use_cache: bool = True) -> ExtractionResult:
    """Extract a palette from *image_path* and map it to ``sys_color_*`` tokens.

    Unlike :func:`generate_wallust_tokens` this never substitutes the built-in
    palette: a failure comes back as a failure, so the caller can keep the
    colours it has and try again later instead of treating navy as the
    wallpaper's palette.
    """
    resolved = resolve_wallpaper_path(image_path)
    if not resolved:
        LOG.debug("No readable wallpaper image at %r", image_path)
        return ExtractionResult(error="Could not read the wallpaper image.")

    fingerprint = _file_fingerprint(resolved)
    if use_cache:
        cache = _read_cache()
        if cache and cache.get("fingerprint") == fingerprint:
            global _memory_cache
            _memory_cache = cache
            return ExtractionResult(tokens=dict(cache["tokens"]))

    palette = _extract_palette(resolved)
    if not palette and _cancelled.is_set():
        return ExtractionResult(cancelled=True)
    if not palette:
        global _warned_failure
        have_wallust = bool(find_wallust())
        if not _warned_failure:
            _warned_failure = True
            LOG.warning("Could not extract colours from %s.", resolved)
        return ExtractionResult(
            error=(
                "Could not read colours from the wallpaper."
                if have_wallust
                else "wallust was not found."
            )
        )

    tokens = palette_to_tokens(palette)
    _write_cache(fingerprint, resolved, tokens)
    return ExtractionResult(tokens=tokens)


def generate_wallust_tokens(image_path: str, *, use_cache: bool = True) -> Dict[str, str]:
    """Like :func:`extract_wallust_tokens`, but always returns a full palette.

    Falls back to the built-in palette on failure, for callers (such as the
    bundle self-check) that need some palette and compare against the default.
    """
    result = extract_wallust_tokens(image_path, use_cache=use_cache)
    return dict(result.tokens) if result.ok else dict(DEFAULT_TOKENS)


# --------------------------------------------------------------------------
# Colour maths and palette mapping
# --------------------------------------------------------------------------

def _parse_hex(value: str) -> Optional[tuple[int, int, int]]:
    if not isinstance(value, str):
        return None
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        return None
    try:
        return tuple(int(text[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return None


def _normalise_hex(value: str) -> str:
    rgb = _parse_hex(value)
    if rgb is None:
        return value
    return _rgb_to_hex(rgb)


def _rgb_to_hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        *(max(0, min(255, int(round(component)))) for component in rgb)
    )


def _rgba(value: str, alpha: float) -> str:
    rgb = _parse_hex(value)
    if rgb is None:
        return f"rgba(0, 0, 0, {alpha})"
    return f"rgba({rgb[0]}, {rgb[1]}, {rgb[2]}, {alpha})"


def _mix(first: str, second: str, amount: float) -> str:
    one = _parse_hex(first)
    two = _parse_hex(second)
    if one is None or two is None:
        return first
    return _rgb_to_hex(
        one[index] * (1 - amount) + two[index] * amount for index in range(3)
    )


def _relative_luminance(value: str) -> float:
    rgb = _parse_hex(value)
    if rgb is None:
        return 0.0

    def linear(channel: int) -> float:
        channel = channel / 255.0
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    red, green, blue = (linear(channel) for channel in rgb)
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(first: str, second: str) -> float:
    a = _relative_luminance(first)
    b = _relative_luminance(second)
    lighter, darker = max(a, b), min(a, b)
    return (lighter + 0.05) / (darker + 0.05)


def _hls(value: str):
    rgb = _parse_hex(value)
    if rgb is None:
        return 0.0, 0.5, 0.0
    return colorsys.rgb_to_hls(*(channel / 255.0 for channel in rgb))


def _with_lightness(value: str, lightness: float, saturation: Optional[float] = None) -> str:
    hue, _, sat = _hls(value)
    if saturation is not None:
        sat = saturation
    return _rgb_to_hex(
        component * 255 for component in colorsys.hls_to_rgb(hue, lightness, sat)
    )


def _ensure_contrast(value: str, background: str, minimum: float, target: str = "#ffffff") -> str:
    if _contrast(value, background) >= minimum:
        return value
    for step in (0.15, 0.3, 0.45, 0.6, 0.75, 0.9, 1.0):
        candidate = _mix(value, target, step)
        if _contrast(candidate, background) >= minimum:
            return candidate
    return target


def _text_on(background: str) -> str:
    dark = "#081018"
    light = "#ffffff"
    return dark if _contrast(dark, background) >= _contrast(light, background) else light


def _pick_accent(palette: Dict[str, str]) -> str:
    candidates = [palette.get(f"color{index}") for index in range(1, 7)]
    candidates += [palette.get("cursor"), palette.get("foreground")]

    best = None
    best_score = -1.0
    for candidate in candidates:
        rgb = _parse_hex(candidate or "")
        if rgb is None:
            continue
        _, saturation, value = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in rgb))
        if saturation < 0.12:
            continue
        score = saturation * 0.7 + value * 0.3
        if value < 0.25:
            score *= 0.35
        if score > best_score:
            best, best_score = candidate, score

    return best or palette.get("color6") or palette.get("foreground") or DEFAULT_TOKENS["sys_color_primary"]


def _pick_semantic(palette: Dict[str, str], hue_ranges, fallback: str) -> str:
    for key in (*[f"color{index}" for index in range(1, 7)], "cursor"):
        value = palette.get(key)
        rgb = _parse_hex(value or "")
        if rgb is None:
            continue
        hue, saturation, brightness = colorsys.rgb_to_hsv(*(channel / 255.0 for channel in rgb))
        hue *= 360.0
        if saturation < 0.3 or brightness < 0.25:
            continue
        if any(low <= hue <= high for low, high in hue_ranges):
            return value
    return fallback


def palette_to_tokens(palette: Dict[str, str]) -> Dict[str, str]:
    """Map a wallust palette to the app's semantic ``sys_color_*`` tokens.

    wallust's ``foreground`` is the most salient color and can be vivid pink or
    yellow, which is unreadable as body text. Text tokens are therefore derived
    as a near-white ramp tinted by the accent, then checked for contrast.
    """
    accent = _pick_accent(palette)

    # Dynamic mode is always dark. Clamp the extracted background accordingly.
    hue, lightness, saturation = _hls(palette.get("background", DEFAULT_TOKENS["sys_color_background"]))
    background = _rgb_to_hex(
        component * 255
        for component in colorsys.hls_to_rgb(hue, min(lightness, 0.075), min(saturation, 0.55))
    )

    hue, lightness, saturation = _hls(accent)
    accent = _rgb_to_hex(
        component * 255
        for component in colorsys.hls_to_rgb(
            hue,
            min(0.74, max(0.56, lightness)),
            min(0.92, max(0.45, saturation)),
        )
    )

    surface = _mix(background, accent, 0.07)
    surface_raised = _mix(background, accent, 0.12)
    border = _mix(background, accent, 0.55)
    border_muted = _mix(background, accent, 0.22)

    primary = _ensure_contrast(accent, background, 3.0)
    primary_light = _ensure_contrast(
        _with_lightness(primary, max(0.78, _hls(primary)[1])), surface, 4.5
    )
    text_main = _ensure_contrast(_mix("#ffffff", accent, 0.08), surface, 7.0)
    text_secondary = _ensure_contrast(_mix(text_main, background, 0.30), surface, 4.5)
    text_highlight = _ensure_contrast(
        _with_lightness(primary, max(0.84, _hls(primary)[1])), surface, 4.5
    )
    # Muted text is only used for disabled controls, which WCAG exempts;
    # 3:1 keeps them visibly inactive but still legible.
    text_muted = _ensure_contrast(_mix(text_main, background, 0.55), surface, 3.0)

    # Translucent fills, flattened onto the surface they are drawn over, so
    # the text on them can be checked: button text sits on primary_dim, and
    # highlighted or selected text sits on primary_hover.
    dim_fill = _mix(surface, primary, 0.10)
    hover_fill = _mix(surface, primary, 0.20)
    primary_light = _ensure_contrast(primary_light, dim_fill, 4.5)
    text_highlight = _ensure_contrast(text_highlight, hover_fill, 4.5)
    selection_text = _ensure_contrast(_text_on(border), hover_fill, 4.5)

    success = _pick_semantic(palette, ((85, 175),), "#4ade80")
    success = _ensure_contrast(success, surface, 4.5)
    alert = _pick_semantic(palette, ((0, 25), (330, 360)), "#ff6b8a")
    alert = _ensure_contrast(alert, surface, 4.5)

    return {
        "sys_color_background": background,
        "sys_color_surface": surface,
        "sys_color_surface_raised": surface_raised,
        "sys_color_border": border,
        "sys_color_border_muted": border_muted,
        "sys_color_primary": primary,
        "sys_color_primary_light": primary_light,
        "sys_color_primary_dim": _rgba(primary, 0.10),
        "sys_color_primary_hover": _rgba(primary, 0.20),
        "sys_color_primary_pressed": _rgba(primary, 0.30),
        "sys_color_text_main": text_main,
        "sys_color_text_secondary": text_secondary,
        "sys_color_text_highlight": text_highlight,
        "sys_color_text_muted": text_muted,
        "sys_color_on_primary": _text_on(primary),
        "sys_color_button_text": primary_light,
        "sys_color_success": success,
        "sys_color_alert": alert,
        "sys_color_on_alert": _text_on(alert),
        "sys_color_disabled_surface": "rgba(255, 255, 255, 0.03)",
        "sys_color_selection_text": selection_text,
    }

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from system_sl.utils.autostart import CrossPlatformAutostart

log = logging.getLogger(__name__)

EXT_UUID = "focused-window-dbus@flexagoon.com"
DBUS_OBJECT_PATH = "/org/gnome/shell/extensions/FocusedWindow"

CONFIG_DIR = Path.home() / ".config" / "system-sl"
SETTINGS_FILE = CONFIG_DIR / "aw_settings.json"
PID_FILE = CONFIG_DIR / "aw_daemon.pid"


def _resolve_bin(name: str) -> str | None:
    appdir = os.environ.get("APPDIR")
    if appdir:
        candidate = Path(appdir) / "usr" / "bin" / name
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name)


def is_aw_server_alive(port: int = 5600) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/0/info", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def is_aw_user_enabled() -> bool:
    """Defaults to True on fresh install until user explicitly turns ActivityWatch OFF."""
    if not SETTINGS_FILE.exists():
        return True
    try:
        return json.loads(SETTINGS_FILE.read_text()).get("enabled", True)
    except Exception:
        return True


def set_aw_user_enabled(enabled: bool) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text())
        except Exception:
            pass
    data["enabled"] = enabled
    SETTINGS_FILE.write_text(json.dumps(data, indent=2))


def ensure_gnome_wayland_extension() -> str:
    """Auto-install and enable Focused Window D-Bus on GNOME Wayland.
    Returns: 'not_needed' | 'active' | 'needs_relogin'
    """
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()

    if session != "wayland" or "gnome" not in desktop:
        return "not_needed"

    appdir = os.environ.get("APPDIR")
    if appdir:
        src_dir = Path(appdir) / "usr" / "share" / "gnome-shell-extensions" / EXT_UUID
        dst_dir = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / EXT_UUID
        if src_dir.is_dir() and not dst_dir.exists():
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst_dir)
            log.info("Installed GNOME Shell extension: %s", EXT_UUID)

    if shutil.which("gnome-extensions"):
        subprocess.run(["gnome-extensions", "enable", EXT_UUID], capture_output=True, check=False)

    # Verify if GNOME Shell has registered the D-Bus object path
    check = subprocess.run(
        [
            "gdbus", "introspect", "--session",
            "--dest", "org.gnome.Shell",
            "--object-path", DBUS_OBJECT_PATH,
        ],
        capture_output=True,
    )
    return "active" if check.returncode == 0 else "needs_relogin"


def _is_daemon_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        PID_FILE.unlink(missing_ok=True)
        return False


def start_aw_background_process() -> None:
    """Spawn `system-sl --aw-bg` as an independent background process if not already running."""
    if _is_daemon_running() or is_aw_server_alive():
        return

    autostart = CrossPlatformAutostart(app_name="SL-ActivityWatch", exec_args=["--aw-bg"])
    cmd = autostart._get_exec_args()
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )


def enable_activitywatch() -> str:
    """Turn ActivityWatch ON: saves preference, enables autostart, and starts daemon immediately."""
    set_aw_user_enabled(True)
    CrossPlatformAutostart(app_name="SL-ActivityWatch", exec_args=["--aw-bg"]).ensure_synced()
    ext_status = ensure_gnome_wayland_extension()
    start_aw_background_process()
    return ext_status


def disable_activitywatch() -> None:
    """Explicitly turn ActivityWatch OFF: disables autostart and stops daemon + child binaries."""
    set_aw_user_enabled(False)
    CrossPlatformAutostart(app_name="SL-ActivityWatch", exec_args=["--aw-bg"]).disable()

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        finally:
            PID_FILE.unlink(missing_ok=True)


def run_activitywatch_daemon(port: int = 5600) -> None:
    """Entrypoint for `system-sl --aw-bg`."""
    if not is_aw_user_enabled():
        sys.exit(0)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    ensure_gnome_wayland_extension()

    procs: list[subprocess.Popen] = []

    def shutdown(*_):
        for p in reversed(procs):
            if p.poll() is None:
                p.terminate()
        for p in reversed(procs):
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    if not is_aw_server_alive(port):
        server_bin = _resolve_bin("aw-server-rust")
        if server_bin:
            procs.append(subprocess.Popen([server_bin, "--port", str(port)]))
            for _ in range(24):
                if is_aw_server_alive(port):
                    break
                time.sleep(0.25)

    watcher_bin = _resolve_bin("aw-awatcher")
    if watcher_bin:
        procs.append(subprocess.Popen([watcher_bin]))

    # Supervise children and respect explicit user shutoff
    while True:
        time.sleep(5)
        if not is_aw_user_enabled():
            shutdown()
        for idx, p in enumerate(procs):
            if p.poll() is not None:
                procs[idx] = subprocess.Popen(p.args)

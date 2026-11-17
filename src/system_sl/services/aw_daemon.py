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

# Verbose debug flags (set via env var DEBUG_AW=1 to enable)
_DEBUG = os.environ.get("DEBUG_AW", "0") == "1"


def dprint(msg: str, *args) -> None:
    """Print debug state to stderr if DEBUG_AW=1."""
    if _DEBUG:
        timestamp = time.strftime("%H:%M:%S")
        print(f"[AW-DEBUG {timestamp}] {msg % args if args else msg}", file=sys.stderr)


def resolve_bin_with_trace(name: str) -> str | None:
    """Resolve the binary path for an ActivityWatch binary, printing every detail."""
    dprint(f"=== resolving binary: {name} ===")
    appdir = os.environ.get("APPDIR")
    if appdir:
        dprint(f"APPDIR is set: {appdir}")
        candidate = Path(appdir) / "usr" / "bin" / name
        dprint(f"Checking AppDir candidate: {candidate}")
        dprint(f"Candidate exists: {candidate.is_file()}")
        if candidate.is_file():
            dprint(f"✓ Found ActivityWatch binary via APPDIR: {candidate}")
            return str(candidate)
        else:
            dprint(f"✗ Binary NOT found at AppDir path: {candidate}")
    else:
        dprint("APPDIR is not set (running in normal mode)")

    which_result = shutil.which(name)
    dprint(f"shutil.which('{name}') => '{which_result}'")
    if which_result:
        dprint(f"✓ Found via shutil.which: {which_result}")
    else:
        dprint(f"✗ Binary NOT found via shutil.which")
    dprint(f"=== done resolving binary: {name} ===")
    return which_result


def is_aw_server_alive(port: int = 5600) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/0/info", timeout=1) as r:
            return r.status == 200
    except Exception as e:
        dprint(f"Server alive check on port {port} failed: {e}")
        return False


def is_aw_user_enabled() -> bool:
    """Defaults to True on fresh install until user explicitly turns ActivityWatch OFF."""
    if not SETTINGS_FILE.exists():
        dprint("No settings file found, defaulting to enabled=True")
        return True
    try:
        data = json.loads(SETTINGS_FILE.read_text())
        enabled = data.get("enabled", True)
        dprint(f"Settings file enabled={enabled}")
        return enabled
    except Exception as e:
        dprint(f"Error reading settings file: {e}, defaulting to enabled=True")
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
    dprint(f"Set ActivityWatch user enabled to: {enabled}")


def ensure_gnome_wayland_extension() -> str:
    """Auto-install and enable Focused Window D-Bus on GNOME Wayland."""
    dprint("=== ensuring GNOME Wayland extension ===")
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    dprint(f"Session type: {session}, Desktop: {desktop}")

    if session != "wayland" or "gnome" not in desktop:
        dprint("Not Wayland or not GNOME, skipping extension setup")
        return "not_needed"

    appdir = os.environ.get("APPDIR")
    if appdir:
        src_dir = Path(appdir) / "usr" / "share" / "gnome-shell-extensions" / "focused-window-dbus@flexagoon.com"
        dst_dir = Path.home() / ".local" / "share" / "gnome-shell" / "extensions" / "focused-window-dbus@flexagoon.com"
        dprint(f"Looking for extension in AppDir: {src_dir}")
        dprint(f"Source dir exists: {src_dir.is_dir()}")
        dprint(f"Destination exists: {dst_dir.exists()}")
        if src_dir.is_dir() and not dst_dir.exists():
            dst_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_dir, dst_dir)
            dprint("✓ Installed GNOME Shell extension from AppDir")
        else:
            dprint("✗ Could not install GNOME Shell extension")

    if shutil.which("gnome-extensions"):
        dprint("gnome-extensions found, attempting to enable extension")
        subprocess.run(["gnome-extensions", "enable", "focused-window-dbus@flexagoon.com"], capture_output=True, check=False)
    else:
        dprint("gnome-extensions not available")

    # Verify if GNOME Shell has registered the D-Bus object path
    check = subprocess.run(
        [
            "gdbus", "introspect", "--session",
            "--dest", "org.gnome.Shell",
            "--object-path", "/org/gnome/shell/extensions/FocusedWindow",
        ],
        capture_output=True,
    )
    result = "active" if check.returncode == 0 else "needs_relogin"
    dprint(f"GNOME D-Bus introspect result: {result}")
    dprint("=== done ensuring GNOME Wayland extension ===")
    return result


def _is_daemon_running() -> bool:
    if not PID_FILE.exists():
        dprint("No PID file found, daemon not running")
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        dprint(f"PID file contains: {pid}")
        os.kill(pid, 0)
        dprint(f"Process {pid} is running")
        return True
    except (ValueError, ProcessLookupError, PermissionError) as e:
        dprint(f"Error checking PID: {e}, cleaning up PID file")
        PID_FILE.unlink(missing_ok=True)
        return False


def start_aw_background_process() -> None:
    """Spawn `system-sl --aw-bg` as an independent background process if not already running."""
    dprint("=== starting ActivityWatch background process ===")
    if _is_daemon_running() or is_aw_server_alive():
        dprint("Daemon already running, returning early")
        return

    autostart = CrossPlatformAutostart(app_name="SL-ActivityWatch", exec_args=["--aw-bg"])
    cmd = autostart._get_exec_args()
    dprint(f"Autostart command: {cmd}")

    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    dprint(f"Subprocess kwargs: {kwargs}")

    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )
    dprint("Subprocess launched, activitywatch background process started")
    dprint("=== done starting ActivityWatch background process ===")


def enable_activitywatch() -> str:
    """Turn ActivityWatch ON: saves preference, enables autostart, and starts daemon immediately."""
    dprint("=== enabling ActivityWatch ===")
    set_aw_user_enabled(True)
    dprint("User preference set to enabled=True")

    CrossPlatformAutostart(app_name="SL-ActivityWatch", exec_args=["--aw-bg"]).ensure_synced()
    dprint("Autostart synced")

    ext_status = ensure_gnome_wayland_extension()
    dprint(f"GNOME extension status: {ext_status}")

    start_aw_background_process()
    dprint("=== done enabling ActivityWatch ===")
    return ext_status


def disable_activitywatch() -> None:
    """Explicitly turn ActivityWatch OFF: disables autostart and stops daemon + child binaries."""
    dprint("=== disabling ActivityWatch ===")
    set_aw_user_enabled(False)
    dprint("User preference set to enabled=False")

    CrossPlatformAutostart(app_name="SL-ActivityWatch", exec_args=["--aw-bg"]).disable()
    dprint("Autostart disabled")

    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            dprint(f"Terminating daemon PID: {pid}")
            os.kill(pid, signal.SIGTERM)
        except (ValueError, ProcessLookupError, PermissionError):
            pass
        finally:
            PID_FILE.unlink(missing_ok=True)
    dprint("=== done disabling ActivityWatch ===")


def run_activitywatch_daemon(port: int = 5600) -> None:
    """Entrypoint for `system-sl --aw-bg`."""
    dprint(f"=== run_activitywatch_daemon starting, port={port} ===")

    if not is_aw_user_enabled():
        dprint("ActivityWatch disabled by user, exiting")
        sys.exit(0)

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    dprint(f"PID file written: {PID_FILE}")

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


    dprint(f"Checking if AW server is alive on port {port}...")
    if not is_aw_server_alive(port):
        dprint(f"AW server not alive, attempting to start server binary...")
        server_bin = resolve_bin_with_trace("aw-server-rust")
        dprint(f"Resolved server binary: {server_bin}")
        if server_bin:
            dprint(f"Starting server binary: {server_bin} --port {port}")
            procs.append(subprocess.Popen([server_bin, "--port", str(port)]))
            # Wait for server to start, with timeout
            for trial in range(96):  # 96 trials * 0.25s = 24 seconds max
                dprint(f"Waiting for server to start... trial {trial+1}/96")
                if is_aw_server_alive(port):
                    dprint(f"✓ Server is alive after {trial+1} trials")
                    break
                time.sleep(0.25)
            else:
                dprint(f"✗ Server did not become alive after 96 trials (24s timeout)")
        else:
            dprint(f"✗ aw-server-rust binary NOT found; cannot start server")

    # Verify the server is actually reachable (either pre-existing or newly started)
    dprint(f"Verifying server is reachable...")
    if is_aw_server_alive(port):
        dprint("Server is reachable, starting aw-awatcher...")
        watcher_bin = resolve_bin_with_trace("aw-awatcher")
        dprint(f"Resolved watcher binary: {watcher_bin}")
        if watcher_bin:
            dprint(f"Starting watcher: {watcher_bin}")
            procs.append(subprocess.Popen([watcher_bin]))
        else:
            dprint(f"✗ aw-awatcher binary NOT found; skipping aw-awatcher")
    else:
        dprint(f"✗ Server not reachable on port {port}; shutting down")
        shutdown()

    # Supervise children and respect explicit user shutoff
    dprint("Entering main supervision loop...")
    while True:
        dprint("Supervision tick...")
        time.sleep(5)
        if not is_aw_user_enabled():
            dprint("User disabled ActivityWatch, shutting down")
            shutdown()
        for idx, p in enumerate(procs):
            if p.poll() is not None:
                dprint(f"Child process {idx} (args: {p.args}) exited, restarting...")
                procs[idx] = subprocess.Popen(p.args)

EXT_UUID = "focused-window-dbus@flexagoon.com"
DBUS_OBJECT_PATH = "/org/gnome/shell/extensions/FocusedWindow"

CONFIG_DIR = Path.home() / ".config" / "system-sl"
SETTINGS_FILE = CONFIG_DIR / "aw_settings.json"
PID_FILE = CONFIG_DIR / "aw_daemon.pid"
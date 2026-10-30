import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import zipfile
import hashlib
from pathlib import Path

import requests
from PySide6.QtCore import QThread, Signal


GITHUB_REPO = "sproutcake23/System-SL"
GITHUB_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


def get_current_version() -> str:
    """Read the running version.

    Package metadata only exists for a pip install. A PyInstaller build has
    none, and the previous fallback of "0.0.0" made every newer release look
    like an upgrade, so auto-update nagged forever.
    """
    try:
        from importlib.metadata import version
        return version("system-sl")
    except Exception:
        pass
    try:
        from system_sl import __version__
        return __version__
    except Exception:
        return "0.0.0"


def running_appimage() -> Path | None:
    """Return the AppImage this process was launched from, if any."""
    appimage = os.environ.get("APPIMAGE")
    if not appimage:
        return None
    path = Path(appimage)
    return path if path.is_file() else None


def get_latest_release_info() -> dict:
    """Fetch the latest release info from GitHub."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    resp = requests.get(GITHUB_API_URL, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_version(tag: str) -> tuple:
    """Parse a version tag like 'v0.2.0' into a comparable tuple (0, 2, 0)."""
    tag = tag.lstrip("v").strip()
    parts = tag.split(".")
    return tuple(int(p) for p in parts)


def should_update(current: str, latest: str) -> bool:
    """Return True if `latest` version is newer than `current`."""
    try:
        return parse_version(latest) > parse_version(current)
    except (ValueError, IndexError):
        return False


def get_platform_asset(assets: list) -> dict | None:
    """Pick the correct release asset for the current OS.

    An AppImage user must receive the AppImage. Falling back to the bare
    tar.xz for them is what used to happen, and installing that next to
    their AppImage left two unrelated copies of the app on one machine.
    """
    is_windows = os.name == "nt"
    if is_windows:
        for asset in assets:
            if asset["name"].lower().endswith(".zip"):
                return asset
        return None

    suffix = ".appimage" if running_appimage() else ".tar.xz"
    for asset in assets:
        if asset["name"].lower().endswith(suffix):
            return asset
    return None


def find_checksums_asset(assets: list) -> dict | None:
    """Find a release asset containing SHA256 checksums for the release."""
    for asset in assets:
        name = asset["name"].lower()
        if name.endswith((".sha256", ".sha256sum", "sha256sums.txt", "checksums.txt")):
            return asset
    return None


def verify_checksum(file_path: Path, expected_hex: str) -> bool:
    """Return True if the SHA-256 of ``file_path`` matches ``expected_hex``."""
    expected_hex = expected_hex.strip().lower()
    # A checksums line may be "<hash>  <filename>" or "<hash>  <path>/<file>"
    expected_hex = expected_hex.split()[0]
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest() == expected_hex


def parse_checksums(text: str, asset_name: str) -> str | None:
    """Extract the expected SHA-256 for ``asset_name`` from checksums text."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1]
        # name may be prefixed with a path; match on basename
        if name == asset_name or name.endswith("/" + asset_name) or name == "./" + asset_name:
            return digest
    return None


def stage_appimage_update(new_appimage: Path, current: Path) -> Path:
    """Stage a replacement AppImage next to the running one.

    The current image cannot be overwritten in place while it is mounted, so
    the new file is dropped alongside it and apply_pending_update() swaps it
    in on the next launch.
    """
    staged = current.with_name(current.name + ".new")
    shutil.copy2(new_appimage, staged)
    os.chmod(staged, 0o755)
    _save_pending_update(staged, new_appimage.name)
    return staged


# Release asset names, e.g. system-sl-1.4.3-x86_64.AppImage. An AppImage the
# user renamed themselves keeps their name across updates.
_VERSIONED_APPIMAGE_NAME = re.compile(r"system-sl-(\d+(?:\.\d+)*)-x86_64\.AppImage")


def appimage_version(name: str) -> tuple | None:
    """Return the version in a release AppImage name, or None if renamed."""
    match = _VERSIONED_APPIMAGE_NAME.fullmatch(name)
    return parse_version(match.group(1)) if match else None


def apply_pending_update() -> None:
    """Swap in a staged AppImage replacement and relaunch into it.

    Runs before any UI starts. Renaming over the old image is safe even while
    it is mounted: FUSE keeps the previous inode open for the running process.
    That process is still the old version, so it then execs the new image;
    otherwise the update would only take effect on the launch after next.
    """
    staged, new_name = _load_pending_update()
    if staged is None:
        return

    current = running_appimage()
    if current is None or not staged.is_file():
        _clear_pending_update()
        return

    target = current
    if (
        new_name
        and _VERSIONED_APPIMAGE_NAME.fullmatch(current.name)
        and _VERSIONED_APPIMAGE_NAME.fullmatch(new_name)
    ):
        target = current.with_name(new_name)

    try:
        os.replace(staged, target)
    except OSError as e:
        # Keep the pending record and the staged file so the next launch can
        # retry. Clearing the record here used to strand the .new file.
        print(f"[update] Could not apply staged update: {e}")
        return
    _clear_pending_update()

    try:
        os.chmod(target, 0o755)
        if target != current:
            current.unlink(missing_ok=True)
            from system_sl.utils.appimage_install import appimage_moved
            appimage_moved(target)
    except OSError as e:
        print(f"[update] Applied update but could not tidy up: {e}")
    print(f"[update] Applied staged update to {target}")

    _relaunch(target)


def _relaunch(appimage: Path) -> None:
    """Replace this process with ``appimage``, keeping the arguments."""
    env = os.environ.copy()
    # This process runs inside a PyInstaller onefile bundle. Without a reset,
    # the new bundle's bootloader inherits our state and does not unpack itself.
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env.pop("LD_LIBRARY_PATH_ORIG")
    else:
        env.pop("LD_LIBRARY_PATH", None)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execve(appimage, [str(appimage), *sys.argv[1:]], env)
    except OSError as e:
        # The update is on disk either way; it runs from the next launch.
        print(f"[update] Could not relaunch {appimage}: {e}")


def _save_pending_update(staged: Path, new_name: str) -> None:
    _write_settings({"pending_appimage_update": str(staged), "pending_appimage_name": new_name})


def _load_pending_update() -> tuple[Path | None, str | None]:
    settings = _read_settings()
    raw = settings.get("pending_appimage_update")
    return (Path(raw) if raw else None), settings.get("pending_appimage_name")


def _clear_pending_update() -> None:
    _write_settings({"pending_appimage_update": None, "pending_appimage_name": None})


def download_asset(url: str, dest_dir: Path) -> Path:
    """Stream-download a release asset to dest_dir. Returns the file path."""
    resp = requests.get(url, stream=True, timeout=120)
    resp.raise_for_status()
    filename = url.split("/")[-1]
    dest_file = dest_dir / filename
    with open(dest_file, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return dest_file


def extract_binary(archive_path: Path, dest_dir: Path) -> Path:
    """Extract the system-sl binary from a tar.xz or zip archive."""
    binary_name = "system-sl.exe" if os.name == "nt" else "system-sl"

    if archive_path.suffix == ".xz" or archive_path.name.endswith(".tar.xz"):
        with tarfile.open(archive_path, "r:xz") as tar:
            # filter="data" blocks path-traversal (absolute/.. paths) and is
            # the safe default; older Pythons (<3.12) lack the kwarg so we
            # fall back to the plain extractall.
            try:
                tar.extractall(path=dest_dir, filter="data")
            except TypeError:
                tar.extractall(path=dest_dir)
    elif archive_path.suffix == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_dir)
    else:
        raise ValueError(f"Unsupported archive format: {archive_path.name}")

    for f in dest_dir.rglob(binary_name):
        if f.is_file():
            return f

    raise FileNotFoundError(f"Binary '{binary_name}' not found inside {archive_path.name}")


def binary_install_path() -> Path:
    """Where a bare-binary update is written.

    A frozen build replaces the copy that is running, so shortcuts and the
    autostart entry stay valid. Updates used to go to a fixed folder, and on
    Windows that was %APPDATA% while the installer uses %LOCALAPPDATA%: every
    update left a second copy of the app behind.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "system-sl" / "system-sl.exe"
    return Path.home() / ".local" / "bin" / "system-sl"


def write_windows_shortcut(shortcut_file: Path, target: Path) -> None:
    """Create or overwrite a .lnk shortcut that launches ``target``."""
    import subprocess
    ps_script = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_file}")
    $Shortcut.TargetPath = "{target}"
    $Shortcut.WorkingDirectory = "{target.parent}"
    $Shortcut.Save()
    """
    subprocess.run(
        ["powershell.exe", "-Command", ps_script], capture_output=True, shell=True
    )


def install_update(new_binary: Path) -> None:
    """Replace the installed binary and update the desktop shortcut."""
    target_path = binary_install_path()
    target_path.parent.mkdir(parents=True, exist_ok=True)

    # Copy beside the target, then rename over it. Writing into a running
    # executable fails on Linux ("Text file busy") and on Windows.
    staged = target_path.with_name(target_path.name + ".new")
    shutil.copy2(new_binary, staged)
    if os.name != "nt":
        os.chmod(staged, 0o755)
    else:
        # Windows will not replace a running executable but will rename it.
        # The .old file is deleted on the next launch.
        old = target_path.with_name(target_path.name + ".old")
        old.unlink(missing_ok=True)
        if target_path.exists():
            os.replace(target_path, old)
    os.replace(staged, target_path)

    if os.name != "nt":
        shortcut_file = Path.home() / ".local" / "share" / "applications" / "system-sl.desktop"
        shortcut_file.parent.mkdir(parents=True, exist_ok=True)
        content = [
            "[Desktop Entry]",
            "Type=Application",
            "Name=THE SYSTEM",
            f"Exec={target_path}",
            f"Path={target_path.parent}",
            "Terminal=false",
            "Icon=utilities-terminal",
            "Categories=Utility;",
            "Comment=Arise, Player.",
        ]
        with open(shortcut_file, "w") as f:
            f.write("\n".join(content))
        os.chmod(shortcut_file, 0o755)
    else:
        desktop = Path(os.environ["USERPROFILE"]) / "Desktop"
        write_windows_shortcut(desktop / "THE SYSTEM.lnk", target_path)


def _settings_path() -> Path:
    from system_sl.utils.paths import get_tasks_file_path
    return Path(get_tasks_file_path("settings.json"))


def _read_settings() -> dict:
    """Read settings.json, tolerating a missing or corrupt file."""
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_settings(values: dict) -> None:
    """Merge ``values`` into settings.json, removing keys set to None."""
    data = _read_settings()
    for key, value in values.items():
        if value is None:
            data.pop(key, None)
        else:
            data[key] = value
    try:
        path = _settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def load_auto_update_preference() -> bool:
    """Read the auto-update setting from settings.json."""
    return bool(_read_settings().get("auto_update", False))


def save_auto_update_preference(enabled: bool) -> None:
    """Persist the auto-update setting to settings.json."""
    _write_settings({"auto_update": enabled})


def _load_last_check_time() -> float:
    """Read the last update check timestamp from settings.json."""
    return _read_settings().get("last_update_check", 0)


def _save_last_check_time() -> None:
    """Persist the current time as the last update check timestamp."""
    import time as _time
    _write_settings({"last_update_check": _time.time()})


class UpdateCheckThread(QThread):
    """Background thread that checks, downloads, and installs updates."""

    update_found = Signal(str, str)
    update_complete = Signal(bool, str)
    progress = Signal(int)

    def run(self):
        try:
            import time as _time

            # Throttle: skip if checked within 24 hours
            if _time.time() - _load_last_check_time() < 86400:
                return

            current = get_current_version()
            try:
                release = get_latest_release_info()
            except requests.RequestException:
                # Offline, rate-limited or GitHub unavailable: retry on the
                # next launch without a popup.
                return
            # Only a check GitHub answered counts. Saving before the request
            # meant an offline or rate-limited launch blocked checks for a day.
            _save_last_check_time()
            latest_tag = release.get("tag_name", "")
            assets = release.get("assets", [])

            if not should_update(current, latest_tag):
                return

            self.update_found.emit(latest_tag, release.get("html_url", ""))

            asset = get_platform_asset(assets)
            if not asset:
                self.update_complete.emit(False, "No compatible release asset found.")
                return

            download_url = asset["browser_download_url"]
            appimage = running_appimage()
            with tempfile.TemporaryDirectory() as tmp:
                tmp_path = Path(tmp)
                self.progress.emit(25)
                archive = download_asset(download_url, tmp_path)
                self.progress.emit(50)
                # Supply-chain verification: confirm the downloaded asset
                # matches the SHA-256 published in the release checksums.
                checksums_asset = find_checksums_asset(assets)
                if checksums_asset is not None:
                    try:
                        checksums_text = requests.get(
                            checksums_asset["browser_download_url"], timeout=30
                        ).text
                    except requests.RequestException as e:
                        self.update_complete.emit(
                            False, f"Update failed: could not fetch release checksums: {e}"
                        )
                        return
                    expected = parse_checksums(checksums_text, asset["name"])
                    if expected is None:
                        self.update_complete.emit(
                            False,
                            f"Update failed: no checksum entry for {asset['name']} in release checksums.",
                        )
                        return
                    if not verify_checksum(archive, expected):
                        self.update_complete.emit(
                            False,
                            "Update failed: SHA-256 checksum mismatch — the download may be corrupt or tampered.",
                        )
                        return
                if appimage is not None:
                    # The AppImage is already the artifact; unpacking it would
                    # only produce a stray bare binary.
                    stage_appimage_update(archive, appimage)
                else:
                    binary = extract_binary(archive, tmp_path)
                    self.progress.emit(75)
                    install_update(binary)
                self.progress.emit(100)

            if appimage is not None:
                self.update_complete.emit(
                    True,
                    f"Update to {latest_tag} downloaded. It will be applied the "
                    "next time you launch THE SYSTEM.",
                )
            else:
                self.update_complete.emit(True, f"Updated to {latest_tag}. Please restart the app.")

        except requests.exceptions.HTTPError as e:
            # Rate limit hit — fail silently, no popup
            if e.response is not None and e.response.status_code == 403:
                return
            self.update_complete.emit(False, f"Update failed: {e}")
        except Exception as e:
            self.update_complete.emit(False, f"Update failed: {e}")

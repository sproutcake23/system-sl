import logging
import os
import platform
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path


def desktop_exec_quote(arg: str) -> str:
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in str(arg))
    return '"' + escaped.replace("%", "%%") + '"'


log = logging.getLogger(__name__)


class CrossPlatformAutostart:
    def __init__(self, app_name: str = "SL-Notifier", exec_args: list[str] | None = None):
        self.app_name = app_name
        self.exec_args = exec_args if exec_args is not None else ["--bg"]
        self.os_type = platform.system()

    def _get_exec_args(self) -> list[str]:
        if getattr(sys, "frozen", False):
            appimage = os.environ.get("APPIMAGE")
            if appimage and os.path.isfile(appimage):
                return [os.path.abspath(appimage), *self.exec_args]
            return [os.path.abspath(sys.executable), *self.exec_args]

        script_path = os.path.abspath(sys.argv[0])
        if not script_path.endswith(".py"):
            return [script_path, *self.exec_args]
        return [os.path.abspath(sys.executable), script_path, *self.exec_args]

    def _get_exec_cmd(self) -> str:
        program, *rest = self._get_exec_args()
        parts = [f'"{program}"'] + [
            f'"{a}"' if not a.startswith("-") else a for a in rest
        ]
        return " ".join(parts)

    def _desktop_exec(self) -> str:
        return " ".join(
            a if a.startswith("-") else desktop_exec_quote(a)
            for a in self._get_exec_args()
        )

    def recorded_command(self) -> str | None:
        try:
            if self.os_type == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                value, _ = winreg.QueryValueEx(key, self.app_name)
                return value

            elif self.os_type == "Linux":
                desktop_path = os.path.expanduser(f"~/.config/autostart/{self.app_name.lower()}.desktop")
                with open(desktop_path) as f:
                    for line in f:
                        if line.startswith("Exec="):
                            return line[len("Exec="):].strip().replace("%%", "%")

            elif self.os_type == "Darwin":
                plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
                with open(plist_path, "rb") as f:
                    return shlex.join(plistlib.load(f).get("ProgramArguments", []))
        except (OSError, plistlib.InvalidFileException):
            pass
        return None

    def recorded_executable(self) -> Path | None:
        command = self.recorded_command()
        if not command:
            return None
        try:
            parts = shlex.split(command, posix=self.os_type != "Windows")
        except ValueError:
            return None
        return Path(parts[0].strip('"')) if parts else None

    def is_enabled(self) -> bool:
        if self.os_type == "Windows":
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                winreg.QueryValueEx(key, self.app_name)
                return True
            except FileNotFoundError:
                return False

        elif self.os_type == "Linux":
            autostart_dir = os.path.expanduser("~/.config/autostart")
            return os.path.exists(os.path.join(autostart_dir, f"{self.app_name.lower()}.desktop"))

        elif self.os_type == "Darwin":
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
            return os.path.exists(plist_path)

        return False

    def enable(self) -> bool:
        exec_cmd = self._get_exec_cmd()
        exec_args = self._get_exec_args()

        if self.os_type == "Windows":
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, exec_cmd)
                return True
            except Exception as e:
                log.error("Windows Registry error: %s", e)
                return False

        elif self.os_type == "Linux":
            autostart_dir = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart_dir, exist_ok=True)
            desktop_path = os.path.join(autostart_dir, f"{self.app_name.lower()}.desktop")

            base_dir = ""
            if not getattr(sys, "frozen", False):
                base_dir = os.path.abspath(os.path.dirname(sys.argv[0]))
                while base_dir != "/" and not os.path.exists(os.path.join(base_dir, ".venv")):
                    base_dir = os.path.dirname(base_dir)
                if base_dir == "/":
                    base_dir = os.path.abspath(os.path.dirname(sys.argv[0]))

            exec_cmd = self._desktop_exec()
            path_line = f"Path={base_dir}\n" if base_dir else ""
            try:
                with open(desktop_path, "w") as f:
                    f.write(
                        "[Desktop Entry]\n"
                        "Type=Application\n"
                        f"Name={self.app_name}\n"
                        f"Exec={exec_cmd}\n"
                        + path_line
                        + "Hidden=false\n"
                        "NoDisplay=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
                return True
            except Exception as e:
                log.error("Linux Autostart error: %s", e)
                return False

        elif self.os_type == "Darwin":
            label = f"com.{self.app_name.lower()}"
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
            plist = {
                "Label": label,
                "ProgramArguments": exec_args,
                "RunAtLoad": True,
            }
            try:
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                if os.environ.get("XPC_SERVICE_NAME") == label:
                    with open(plist_path, "wb") as f:
                        plistlib.dump(plist, f)
                    return True
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", plist_path], capture_output=True)
                with open(plist_path, "wb") as f:
                    plistlib.dump(plist, f)
                loaded = subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path], capture_output=True)
                if loaded.returncode != 0:
                    log.error("macOS launchctl bootstrap failed: %s", loaded.stderr.decode(errors="replace"))
                    return False
                return True
            except Exception as e:
                log.error("macOS LaunchAgent error: %s", e)
                return False

        return False

    def disable(self) -> bool:
        if self.os_type == "Windows":
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                winreg.DeleteValue(key, self.app_name)
                return True
            except FileNotFoundError:
                return True
            except Exception as e:
                log.error("Windows Registry error: %s", e)
                return False

        elif self.os_type == "Linux":
            desktop_path = os.path.expanduser(f"~/.config/autostart/{self.app_name.lower()}.desktop")
            try:
                os.remove(desktop_path)
            except FileNotFoundError:
                pass
            except OSError as e:
                log.error("Could not remove the autostart entry %s: %s", desktop_path, e)
                return False
            return True

        elif self.os_type == "Darwin":
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
            if os.path.exists(plist_path):
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", plist_path], capture_output=True)
                os.remove(plist_path)
            return True

        return False

    def toggle(self) -> bool:
        if self.is_enabled():
            return not self.disable()
        return self.enable()

    def ensure_synced(self) -> bool:
        """Enable autostart if missing, or repair the executable path if the AppImage moved."""
        if not self.is_enabled():
            return self.enable()
        recorded = self.recorded_executable()
        current = Path(self._get_exec_args()[0])
        if recorded != current:
            return self.enable()
        return True

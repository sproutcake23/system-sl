import logging
import os
import platform
import plistlib
import shlex
import sys
import subprocess
from pathlib import Path

def desktop_exec_quote(arg: str) -> str:
    """Quote one argument for a Desktop Entry ``Exec=`` key.

    Per the Desktop Entry spec, a quoted argument escapes ``"``, `` ` ``,
    ``$`` and ``\\`` with a backslash, and a literal ``%`` is written ``%%``
    (field codes). ``Path=`` is a plain string and must not be quoted.
    """
    escaped = "".join("\\" + c if c in '"`$\\' else c for c in str(arg))
    return '"' + escaped.replace("%", "%%") + '"'


log = logging.getLogger(__name__)


class CrossPlatformAutostart:
    def __init__(self, app_name="SL-Notifier"):
        self.app_name = app_name
        self.os_type = platform.system()

    def _get_exec_args(self) -> list[str]:
        """The program and arguments that start the background notifier."""
        if getattr(sys, "frozen", False):
            # Under an AppImage, sys.executable points inside the transient FUSE
            # mount ($APPIMAGE is running, sys.executable is
            # /tmp/.mount_system-slXXXXXX/usr/bin/system-sl), which is unmounted
            # as soon as the app exits. Recording that path produces an
            # autostart entry that can never launch anything again.
            appimage = os.environ.get("APPIMAGE")
            if appimage and os.path.isfile(appimage):
                return [os.path.abspath(appimage), "--bg"]
            return [os.path.abspath(sys.executable), "--bg"]

        script_path = os.path.abspath(sys.argv[0])
        if not script_path.endswith(".py"):
            return [script_path, "--bg"]
        return [os.path.abspath(sys.executable), script_path, "--bg"]

    def _get_exec_cmd(self) -> str:
        """The Windows Run-key command line: every path double-quoted."""
        program, *rest = self._get_exec_args()
        parts = [f'"{program}"'] + [
            f'"{a}"' if a != "--bg" else a for a in rest
        ]
        return " ".join(parts)

    def _desktop_exec(self) -> str:
        return " ".join(
            a if a == "--bg" else desktop_exec_quote(a) for a in self._get_exec_args()
        )

    def recorded_command(self) -> str | None:
        """Return the command the existing autostart entry launches, if any."""
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

            elif self.os_type == "Darwin":  # macOS
                plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
                with open(plist_path, "rb") as f:
                    return shlex.join(plistlib.load(f).get("ProgramArguments", []))
        except (OSError, plistlib.InvalidFileException):
            pass
        return None

    def recorded_executable(self) -> Path | None:
        """Return the program the existing autostart entry launches, if any."""
        command = self.recorded_command()
        if not command:
            return None
        # Windows paths use backslashes, which POSIX splitting would eat.
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

        elif self.os_type == "Darwin":  # macOS
            plist_path = os.path.expanduser(
                f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist"
            )
            return os.path.exists(plist_path)

        return False

    def enable(self) -> bool:
        exec_cmd = self._get_exec_cmd()
        exec_args = self._get_exec_args()

        if self.os_type == "Windows":
            import winreg
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0,
                    winreg.KEY_SET_VALUE,
                )
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, exec_cmd)
                return True
            except Exception as e:
                log.error("Windows Registry error: %s", e)
                return False

        elif self.os_type == "Linux":
            autostart_dir = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart_dir, exist_ok=True)
            desktop_path = os.path.join(
                autostart_dir, f"{self.app_name.lower()}.desktop"
            )

            # 1. Working directory. For a frozen build the "project root" is
            # the AppImage itself (or a /tmp mount that no longer exists), so
            # there is nothing to search for and the key is omitted entirely.
            base_dir = ""
            if not getattr(sys, "frozen", False):
                base_dir = os.path.abspath(os.path.dirname(sys.argv[0]))
                while base_dir != "/" and not os.path.exists(
                    os.path.join(base_dir, ".venv")
                ):
                    base_dir = os.path.dirname(base_dir)
                if base_dir == "/":
                    base_dir = os.path.abspath(os.path.dirname(sys.argv[0]))

            # 2. Exec quotes each argument per the Desktop Entry spec. Path is
            # a plain string: quotes there would become part of the directory
            # name and the launcher could not chdir into it. An autostart
            # entry already runs inside the session, so DISPLAY and the
            # D-Bus address are inherited and do not need to be baked in.
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

        elif self.os_type == "Darwin":  # macOS
            label = f"com.{self.app_name.lower()}"
            plist_path = os.path.expanduser(
                f"~/Library/LaunchAgents/{label}.plist"
            )
            plist = {
                "Label": label,
                "ProgramArguments": exec_args,
                "RunAtLoad": True,
            }
            try:
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                if os.environ.get("XPC_SERVICE_NAME") == label:
                    # Called from the agent itself (the startup autostart
                    # repair in the --bg process): booting it out would kill
                    # this process before bootstrap. Rewrite the plist; it
                    # takes effect at the next login.
                    with open(plist_path, "wb") as f:
                        plistlib.dump(plist, f)
                    return True
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}", plist_path],
                    capture_output=True,
                )
                with open(plist_path, "wb") as f:
                    plistlib.dump(plist, f)
                loaded = subprocess.run(
                    ["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path],
                    capture_output=True,
                )
                if loaded.returncode != 0:
                    log.error(
                        "macOS launchctl bootstrap failed: %s",
                        loaded.stderr.decode(errors="replace"),
                    )
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
                return True  # Already disabled
            except Exception as e:
                log.error("Windows Registry error: %s", e)
                return False

        elif self.os_type == "Linux":
            desktop_path = os.path.expanduser(f"~/.config/autostart/{self.app_name.lower()}.desktop")
            if os.path.exists(desktop_path):
                os.remove(desktop_path)
            return True

        elif self.os_type == "Darwin":  # macOS
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
            if os.path.exists(plist_path):
                # Unload first
                subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", plist_path], capture_output=True)
                os.remove(plist_path)
            return True

        return False

    def toggle(self) -> bool:
        """Enable or disable autostart; return whether it ended up enabled."""
        if self.is_enabled():
            # A failed disable must not be reported as "off".
            return not self.disable()
        return self.enable()
import os
import platform
import sys
import subprocess

class CrossPlatformAutostart:
    def __init__(self, app_name="SL-Notifier"):
        self.app_name = app_name
        self.os_type = platform.system()
        
    def _get_exec_cmd(self) -> str:
        """Determines the correct command to execute this script/binary safely cross-platform."""
        if getattr(sys, "frozen", False):
            # Under an AppImage, sys.executable points inside the transient FUSE
            # mount ($APPIMAGE is running, sys.executable is
            # /tmp/.mount_system-slXXXXXX/usr/bin/system-sl), which is unmounted
            # as soon as the app exits. Recording that path produces an
            # autostart entry that can never launch anything again.
            appimage = os.environ.get("APPIMAGE")
            if appimage and os.path.isfile(appimage):
                return f'"{os.path.abspath(appimage)}" --bg'
            return f'"{os.path.abspath(sys.executable)}" --bg'

        script_path = os.path.abspath(sys.argv[0])
        if not script_path.endswith(".py"):
            return f'"{script_path}" --bg'

        python_exe = os.path.abspath(sys.executable)

        # Every path is quoted. The Exec key is split on unquoted whitespace,
        # so an install directory containing a space would otherwise be torn
        # into two arguments. The previous /bin/sh -c wrapper masked this.
        return f'"{python_exe}" "{script_path}" --bg'
    
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
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
            return os.path.exists(plist_path)
            
        return False

    def enable(self) -> bool:
        exec_cmd = self._get_exec_cmd()

        if self.os_type == "Windows":
            import winreg
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
                # For Windows, we wrap the command in quotes if it contains spaces
                winreg.SetValueEx(key, self.app_name, 0, winreg.REG_SZ, exec_cmd)
                return True
            except Exception as e:
                print(f"Windows Registry Error: {e}")
                return False

        elif self.os_type == "Linux":
            autostart_dir = os.path.expanduser("~/.config/autostart")
            os.makedirs(autostart_dir, exist_ok=True)
            desktop_path = os.path.join(autostart_dir, f"{self.app_name.lower()}.desktop")

            # 1. Working directory. For a frozen build the "project root" is
            # the AppImage itself (or a /tmp mount that no longer exists), so
            # there is nothing to search for and the key is omitted entirely.
            base_dir = ""
            if not getattr(sys, "frozen", False):
                base_dir = os.path.abspath(os.path.dirname(sys.argv[0]))
                while base_dir != "/" and not os.path.exists(os.path.join(base_dir, ".venv")):
                    base_dir = os.path.dirname(base_dir)
                if base_dir == "/":
                    base_dir = os.path.abspath(os.path.dirname(sys.argv[0]))

            # 2. Exec is written verbatim. An autostart entry already runs
            # inside the session, so DISPLAY and the D-Bus address are
            # inherited and do not need to be baked in. Baking them in via
            # /bin/sh -c "VAR=... \"<path>\"" also produced a value with
            # nested double quotes, which the desktop entry spec does not
            # allow and which desktop environments reject or mis-parse.
            try:
                with open(desktop_path, "w") as f:
                    f.write(
                        "[Desktop Entry]\n"
                        "Type=Application\n"
                        f"Name={self.app_name}\n"
                        f"Exec={exec_cmd}\n"
                        + (f"Path={base_dir}\n" if base_dir else "")
                        + "Hidden=false\n"
                        "NoDisplay=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
                return True
            except Exception as e:
                print(f"Linux Autostart Error: {e}")
                return False

        elif self.os_type == "Darwin":  # macOS
            plist_path = os.path.expanduser(f"~/Library/LaunchAgents/com.{self.app_name.lower()}.plist")
            # Break command into components for the plist array
            cmd_args = exec_cmd.split() 
            args_xml = "".join(f"<string>{arg}</string>" for arg in cmd_args)
            
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.{self.app_name.lower()}</string>
    <key>ProgramArguments</key>
    <array>
        {args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
            try:
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                with open(plist_path, "w") as f:
                    f.write(plist_content)
                # Load the agent immediately
                subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", plist_path], capture_output=True)
                return True
            except Exception as e:
                print(f"macOS LaunchAgent Error: {e}")
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
                print(f"Windows Registry Error: {e}")
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
        if self.is_enabled():
            self.disable()
            return False
        else:
            return self.enable()
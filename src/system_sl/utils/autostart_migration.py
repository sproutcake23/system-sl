import os
import sys
import subprocess
from system_sl.utils.autostart import CrossPlatformAutostart # Assuming you saved the new code here
from system_sl.utils.install_cleanup import cleanup_previous_installs

def run_legacy_cleanup():
    """
    Safely purges the old systemd setup from deployed Linux machines.
    Guaranteed not to crash on Windows or macOS.
    """
    # 1. Only run this if we are on Linux and systemctl exists
    if os.name != "posix" or not os.path.exists("/usr/bin/systemctl"):
        return

    legacy_service_name = "sl-notifer.service"
    legacy_file_path = os.path.expanduser(f"~/.config/systemd/user/{legacy_service_name}")

    try:
        # Check if the old systemd service is active or enabled
        check_service = subprocess.run(
            ["systemctl", "--user", "is-enabled", legacy_service_name],
            capture_output=True, text=True
        )
        
        # If systemd knows about it, or the old file still exists, clean it up!
        if "enabled" in check_service.stdout or os.path.exists(legacy_file_path):
            print("[Migration] Legacy systemd service detected. Commencing safe migration...")

            # Stop the old 30-minute interval ghost process immediately
            subprocess.run(["systemctl", "--user", "stop", legacy_service_name], capture_output=True)
            
            # Disable it from starting up on subsequent boots
            subprocess.run(["systemctl", "--user", "disable", legacy_service_name], capture_output=True)
            
            # Permanently delete the physical unit configuration file
            if os.path.exists(legacy_file_path):
                os.remove(legacy_file_path)
                
            # Flush systemd's memory cache so it entirely forgets about the old service
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            print("[Migration] Legacy systemd service completely eradicated.")

    except Exception as e:
        # We catch all exceptions so an unexpected environment doesn't crash the user's entire app
        print(f"[Migration Warning] Failed to clean up legacy systemd artifacts: {e}")


def initialize_application_autostart():
    """
    Handles the clean shift from old systemd to the new cross-platform framework.
    """
    # Step 1: Wipe the old systemd footprints cleanly
    run_legacy_cleanup()

    # Step 1b: Remove old copies and update leftovers, and repoint autostart
    # and menu entries that still launch them
    cleanup_previous_installs()

    # Step 2: Initialize the new architecture
    autostart = CrossPlatformAutostart("SL-Notifier")
    
    # Optional: If your app requires autostart to be forced 'on' by default
    # for all users upon receiving this update, uncomment the lines below:
    # if not autostart.is_enabled():
    #     autostart.enable()
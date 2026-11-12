import os
import subprocess
import shutil
import sys
from pathlib import Path


def is_pip_install() -> bool:
    """Check if running from a pip-installed package."""
    return "site-packages" in str(Path(__file__).parent) or "dist-packages" in str(Path(__file__).parent)


def is_appimage() -> bool:
    """Check if running from an AppImage."""
    return "APPIMAGE" in os.environ


def is_frozen() -> bool:
    """Check if running as PyInstaller frozen bundle."""
    return getattr(sys, "frozen", False)


def get_install_mode() -> str:
    """Returns the installation mode: 'pip', 'appimage', 'frozen', 'dev', or 'unknown'."""
    if is_pip_install():
        return "pip"
    if is_appimage():
        return "appimage"
    if is_frozen():
        return "frozen"
    return "dev"


def migrate_credentials():
    """
    Scans for credentials.json in common locations (Installer folder & Downloads)
    and moves it to the persistent system data directory.
    """
    # 1. Define Potential Sources
    script_dir = Path(__file__).parent.absolute()
    downloads_dir = Path.home() / "Downloads"

    potential_sources = [
        script_dir / "credentials.json",  # Current folder
        downloads_dir / "credentials.json",  # System Downloads folder
    ]

    # 2. Define Destination
    try:
        from system_sl.core.tasks import get_tasks_file_path

        target_path = Path(get_tasks_file_path("credentials.json"))
    except ImportError:
        print("⚠️  Warning: Could not import core.tasks. Migration skipped.")
        return

    # 3. Search and Move
    found_source = None
    for src in potential_sources:
        if src.exists():
            found_source = src
            break

    if found_source:
        print(f"Found Google credentials at: {found_source}")
        print(f"Migrating to: {target_path.parent}...")
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(found_source, target_path)
            print("✅ Credentials migrated successfully.")
        except Exception as e:
            print(f"❌ Error migrating credentials: {e}")
    else:
        print(
            "ℹ️  credentials.json not found in Downloads or current folder. Skipping migration."
        )


def install_sounds(source_sounds_dir: Path):
    """
    Takes the exact folder path where the sounds are currently located,
    and copies them to the system config folder.
    """
    if not source_sounds_dir.exists():
        print(f"ℹ️  No sounds found at {source_sounds_dir}. Skipping.")
        return

    # Try to use your existing path logic
    try:
        from system_sl.core.tasks import get_tasks_file_path

        target_sounds_dir = Path(get_tasks_file_path("sounds"))
    except ImportError:
        # Failsafe just in case the import breaks during install
        target_sounds_dir = Path.home() / ".config" / "system-sl" / "sounds"

    print(f"🎵 Installing notification sounds to {target_sounds_dir}...")

    try:
        target_sounds_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for audio_file in source_sounds_dir.glob("*"):
            if audio_file.is_file():
                shutil.copy2(audio_file, target_sounds_dir)
                count += 1
        print(f"✅ {count} sound(s) installed successfully.")
    except Exception as e:
        print(f"❌ Error installing sounds: {e}")


def create_desktop_shortcut(target_path: Path, install_dir: Path):
    """Creates a .desktop file for Linux."""
    shortcut_dir = Path.home() / ".local" / "share" / "applications"
    shortcut_dir.mkdir(parents=True, exist_ok=True)
    shortcut_file = shortcut_dir / "system-sl.desktop"

    # Use system-wide icon if available, fallback to generic
    icon_path = "system-sl"
    system_icons = [
        Path("/usr/share/icons/hicolor/scalable/apps/system-sl.svg"),
        Path("/usr/local/share/icons/hicolor/scalable/apps/system-sl.svg"),
    ]
    for icon in system_icons:
        if icon.exists():
            icon_path = str(icon)
            break

    content = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=THE SYSTEM",
        f'Exec="{target_path}"',
        f'Path="{install_dir}"',
        "Terminal=false",
        f"Icon={icon_path}",
        "Categories=Utility;Productivity;",
        "Comment=Arise, Player.",
        "StartupNotify=true",
    ]
    with open(shortcut_file, "w") as f:
        f.write("\n".join(content))
    os.chmod(shortcut_file, 0o755)
    print(f"✅ Desktop shortcut created at {shortcut_file}")


def create_windows_shortcut(target_path: Path, install_dir: Path):
    """Creates a Windows shortcut on the desktop."""
    shortcut_file = Path(os.environ["USERPROFILE"]) / "Desktop" / "THE SYSTEM.lnk"
    ps_script = f"""
    $WshShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WshShell.CreateShortcut("{shortcut_file}")
    $Shortcut.TargetPath = "{target_path}"
    $Shortcut.WorkingDirectory = "{install_dir}"
    $Shortcut.Save()
    """
    subprocess.run(
        ["powershell.exe", "-Command", ps_script], capture_output=True, shell=True
    )
    print(f"✅ Windows shortcut created at {shortcut_file}")


def install_the_system():
    binary_name = "system-sl" if os.name != "nt" else "system-sl.exe"
    mode = get_install_mode()

    print(f"🔧 Detected install mode: {mode}")

    # Determine source binary and sounds based on mode
    if mode == "frozen":
        # PyInstaller bundle
        bundle_dir = Path(sys.executable).parent
        source_path = bundle_dir / binary_name
        sounds_folder = bundle_dir / "system_sl" / "assets" / "sounds"
        print("📦 Running from PyInstaller bundle")
    elif mode == "appimage":
        # AppImage - binary is at APPIMAGE path or we're running from extracted AppDir
        appimage_path = Path(os.environ.get("APPIMAGE", ""))
        if appimage_path.exists():
            source_path = appimage_path
        else:
            # Fallback: we might be running from the extracted AppDir
            source_path = Path(sys.executable)
        sounds_folder = Path("/usr/share/system-sl/sounds")
        print("📦 Running from AppImage")
    elif mode == "pip":
        # pip install - no binary to install, sounds are in package data
        print("📦 Running from pip install - no binary installation needed")
        print("✅ Sounds are available via package data")
        print("ℹ️  To create desktop shortcut, run: system-sl --create-desktop")
        migrate_credentials()
        return
    else:
        # Development mode
        bundle_dir = Path(__file__).parent.absolute()
        project_root = bundle_dir.parent.parent.parent
        dev_binary = project_root / "dist" / binary_name

        if dev_binary.exists():
            source_path = dev_binary
            sounds_folder = project_root / "src" / "system_sl" / "assets" / "sounds"
        else:
            print(f"❌ Error: {binary_name} not found in dist/!")
            print("Did you remember to run PyInstaller first?")
            return
        print("📦 Running from development source")

    if not source_path.exists():
        print(f"❌ Error: Binary not found at {source_path}")
        return

    # Install binary to ~/.local/bin (Linux) or %APPDATA%\system-sl (Windows)
    if os.name != "nt":
        install_dir = Path.home() / ".local" / "bin"
    else:
        install_dir = Path(os.environ["APPDATA"]) / "system-sl"

    install_dir.mkdir(parents=True, exist_ok=True)
    target_path = install_dir / binary_name

    print(f"🚚 Copying binary to {target_path}...")
    shutil.copy2(source_path, target_path)

    if os.name != "nt":
        os.chmod(target_path, 0o755)

    # Install sounds (only for frozen/appimage/dev modes)
    if mode in ("frozen", "appimage", "dev") and sounds_folder.exists():
        install_sounds(sounds_folder)

    migrate_credentials()

    # Create desktop shortcut (only for frozen/appimage/dev on Linux)
    if mode in ("frozen", "appimage", "dev") and os.name != "nt":
        print("⚔️  Forging the shortcut...")
        create_desktop_shortcut(target_path, install_dir)
    elif mode in ("frozen", "appimage", "dev") and os.name == "nt":
        print("⚔️  Forging the shortcut...")
        create_windows_shortcut(target_path, install_dir)
    elif mode == "pip":
        print("ℹ️  Desktop shortcut not created for pip install.")
        print("   Run 'system-sl --create-desktop' to create one manually.")

    print("\n✅ INSTALLATION COMPLETE")
    if mode != "pip":
        print(f"Binary installed to: {target_path}")
        print("You can now run 'system-sl' from anywhere.")


def create_desktop_entry():
    """Creates a desktop entry for the current installation (for pip installs)."""
    mode = get_install_mode()
    binary_name = "system-sl" if os.name != "nt" else "system-sl.exe"

    if os.name == "nt":
        print("❌ --create-desktop not supported on Windows (use NSIS installer)")
        return

    # Find the binary
    if mode == "pip":
        # For pip, the binary is the console script entry point
        target_path = Path(sys.executable).parent / binary_name
    else:
        target_path = Path.home() / ".local" / "bin" / binary_name

    if not target_path.exists():
        print(f"❌ Binary not found at {target_path}")
        return

    install_dir = target_path.parent
    create_desktop_shortcut(target_path, install_dir)
    print(f"✅ Desktop shortcut created for {mode} install")


if __name__ == "__main__":
    if "--create-desktop" in sys.argv:
        create_desktop_entry()
    else:
        install_the_system()
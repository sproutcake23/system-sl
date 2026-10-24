import os
import sys
import shutil
from pathlib import Path

# 1. Force the environment to be quiet before Qt even loads
os.environ["AV_LOG_LEVEL"] = "quiet"
os.environ["QT_LOGGING_RULES"] = "*=false"

from PySide6.QtCore import QUrl, QEventLoop, QTimer, qInstallMessageHandler
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from system_sl.utils.json_client import load_data, save_data
from system_sl.utils.paths import get_tasks_file_path, get_sounds_dir

# 2. Native Qt Silencer: Catches any remaining C-level logs and drops them
def _silent_qt_message_handler(mode, context, message):
    pass
qInstallMessageHandler(_silent_qt_message_handler)


DEFAULT_SOUNDS_DIR = get_sounds_dir()
os.makedirs(DEFAULT_SOUNDS_DIR, exist_ok=True)

# The bundled notification cue is default.mp3. There is no default.wav, so the
# old fallback pointed at a file that never existed and every notification
# that fell through to it was silently mute.
SETTINGS_FILE = get_tasks_file_path("settings.json")

_player = None
_audio_output = None

def _get_player():
    global _player, _audio_output
    if _player is None:
        _player = QMediaPlayer()
        _audio_output = QAudioOutput()
        _audio_output.setVolume(1.0)
        _player.setAudioOutput(_audio_output)
    return _player

def get_current_sound() -> str:
    data = load_data(SETTINGS_FILE)
    sound_setting = data.get("notification_sound", "default.mp3")
    
    # If it's just a filename (no path separators), resolve it in the sounds dir
    if os.path.basename(sound_setting) == sound_setting:
        sounds_dir = get_sounds_dir()
        resolved = os.path.join(sounds_dir, sound_setting)
        if os.path.exists(resolved):
            return resolved
        # Fall back to default.mp3 in sounds dir
        return os.path.join(sounds_dir, "default.mp3")
    
    # Legacy absolute path - check if it exists, else fall back
    if os.path.exists(sound_setting):
        return sound_setting
    
    # Fall back to default.mp3 in sounds dir
    return os.path.join(get_sounds_dir(), "default.mp3")

def play_sound(file_path: str):
    player = _get_player()
    player.setSource(QUrl.fromLocalFile(file_path))
    player.play()

def check_audio_duration(file_path: str) -> int:
    player = _get_player()
    player.setSource(QUrl.fromLocalFile(file_path))

    # Wait for the file to load so we can get the duration
    loop = QEventLoop()
    def on_status_changed(status):
        if status in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.InvalidMedia):
            loop.quit()

    player.mediaStatusChanged.connect(on_status_changed)
    QTimer.singleShot(1500, loop.quit)
    loop.exec()
    player.mediaStatusChanged.disconnect(on_status_changed)

    return player.duration()

def set_sound_setting(file_path: str):
    """Save notification sound setting with portable path.
    
    If the sound is from the bundled assets, save just the filename.
    If it's a custom sound, copy it to user sounds dir and save the filename.
    """
    data = load_data(SETTINGS_FILE)
    user_sounds_dir = get_sounds_dir()
    os.makedirs(user_sounds_dir, exist_ok=True)
    
    # Get the bundled sounds directory for comparison
    from system_sl.utils.paths import get_system_data_path
    bundled_sounds_dirs = []
    if getattr(sys, "frozen", False):
        bundle_dir = Path(sys._MEIPASS)
        for pkg_sounds in (
            bundle_dir / "assets" / "sounds",
            bundle_dir / "system_sl" / "assets" / "sounds",
        ):
            if pkg_sounds.exists():
                bundled_sounds_dirs.append(pkg_sounds)
    else:
        pkg_dir = Path(__file__).parent.parent
        pkg_sounds = pkg_dir / "assets" / "sounds"
        if pkg_sounds.exists():
            bundled_sounds_dirs.append(pkg_sounds)
    
    file_path = Path(file_path).resolve()
    filename = file_path.name
    
    # Check if the file is from a bundled sounds directory
    is_bundled = any(file_path.is_relative_to(b) for b in bundled_sounds_dirs)
    
    if is_bundled:
        # Save just the filename for bundled sounds
        data["notification_sound"] = filename
    else:
        # Copy custom sound to user directory and save filename
        dest = user_sounds_dir / filename
        # Handle filename collisions
        counter = 1
        stem = dest.stem
        suffix = dest.suffix
        while dest.exists():
            dest = user_sounds_dir / f"{stem}_{counter}{suffix}"
            counter += 1
            filename = dest.name
        shutil.copy2(file_path, dest)
        data["notification_sound"] = filename
    
    save_data(SETTINGS_FILE, data)

def list_available_sounds() -> list[str]:
    """Returns a list of available sound files in the sounds directory."""
    sounds = []
    if DEFAULT_SOUNDS_DIR.exists():
        for f in DEFAULT_SOUNDS_DIR.glob("*.mp3"):
            sounds.append(str(f))
        for f in DEFAULT_SOUNDS_DIR.glob("*.wav"):
            sounds.append(str(f))
    return sounds
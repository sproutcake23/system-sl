"""Importing the audio module must not resolve sound paths or touch Qt."""

import os
import subprocess
import sys


def test_import_has_no_side_effects(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), QT_QPA_PLATFORM="offscreen")
    code = (
        "import os, sys;"
        "import system_sl.utils.paths as paths;"
        # The package __init__ may already have imported the module; drop it
        # so the import below runs the module code with the spy in place.
        "sys.modules.pop('system_sl.utils.audio_manager', None);"
        "calls = [];"
        "real = paths.get_sounds_dir;"
        "paths.get_sounds_dir = lambda: calls.append(1) or real();"
        "import system_sl.utils.audio_manager as a;"
        # Resolving the sounds folder can create it, so it waits until used.
        "assert calls == [], calls;"
        "assert 'QT_LOGGING_RULES' not in os.environ;"
        "a.initialize_audio();"
        "assert calls;"
        "assert os.environ['QT_LOGGING_RULES'] == '*=false';"
        # The old constant still works for any caller that used it.
        "assert a.DEFAULT_SOUNDS_DIR == a.default_sounds_dir()"
    )
    env.pop("QT_LOGGING_RULES", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

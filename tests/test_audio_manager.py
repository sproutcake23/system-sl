"""Importing the audio module must not touch the filesystem or Qt."""

import os
import subprocess
import sys


def test_import_has_no_side_effects(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), QT_QPA_PLATFORM="offscreen")
    code = (
        "import os;"
        "calls = [];"
        "os.makedirs = lambda *a, **k: calls.append(a);"
        "import system_sl.utils.audio_manager as a;"
        "assert calls == [];"
        "a.initialize_audio();"
        "assert calls"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr

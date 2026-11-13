"""The background notifier must not load the Google client or keyring."""

import os
import subprocess
import sys


def test_notifier_imports_stay_light(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), QT_QPA_PLATFORM="offscreen")
    code = (
        "import sys;"
        "import system_sl.frontend.gui.notifier, system_sl.services.background_service;"
        "heavy = ('googleapiclient', 'google.auth', 'keyring', 'cryptography', 'langchain');"
        "loaded = [m for m in heavy if any(k == m or k.startswith(m + '.') for k in sys.modules)];"
        "assert not loaded, loaded"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr


def test_package_names_still_resolve():
    from system_sl.core import GoogleSyncEngine, load_tasks
    from system_sl.utils import get_tasks_file_path, secure_storage

    assert callable(load_tasks) and callable(get_tasks_file_path)
    assert GoogleSyncEngine.__name__ == "GoogleSyncEngine"
    assert hasattr(secure_storage, "SERVICE_NAME")

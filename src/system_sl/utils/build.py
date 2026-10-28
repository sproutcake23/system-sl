"""Build the standalone PyInstaller executable.

This mirrors the flags used by the release workflow in
``.github/workflows/release.yml`` so a local ``uv run build-sl`` produces a
binary equivalent to the published one. Keep the two in sync.
"""

import subprocess
import sys
from pathlib import Path


def main():
    project_root = Path(__file__).parent.parent.parent.parent
    subprocess.run(
        [
            sys.executable, "-m", "PyInstaller",
            "--name", "system-sl",
            "--windowed",
            "--noconsole",
            "--onefile",
            # Rebuild instead of reusing a stale build/ directory, and never
            # prompt, so the build is reproducible in CI and locally.
            "--noconfirm",
            "--clean",
            "--collect-binaries", "spellchecker",
            "--add-data", "src/system_sl/assets:assets",
            # The prioritisation engine needs the spaCy model at runtime. CI
            # collects it explicitly and this script must too, otherwise a
            # locally built binary silently loses task ranking.
            "--collect-all", "en_core_web_md",
            # AppImage bundles often lack a system CA bundle, which caused
            # SSL CERTIFICATE_VERIFY_FAILED when fetching models. Ship
            # certifi so urllib/requests can verify HTTPS certificates.
            "--collect-all", "certifi",
            str(project_root / "src/system_sl/frontend/gui/main.py"),
        ],
        cwd=project_root,
        check=True,
    )


if __name__ == "__main__":
    main()
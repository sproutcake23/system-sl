"""Build the standalone PyInstaller executable.

The flags live in ``packaging/system-sl.spec``, which the release workflow in
``.github/workflows/release.yml`` also uses, so a local ``uv run build-sl``
produces a binary equivalent to the published one.

On Windows, OCR needs the ``ocr-bundle`` folder that the workflow's "Collect OCR
tools" step creates. Without it, the build warns and OCR will not work.
"""

import subprocess
import sys
from pathlib import Path


def main():
    project_root = Path(__file__).parent.parent.parent.parent
    command = [
        sys.executable, "-m", "PyInstaller",
        str(project_root / "packaging" / "system-sl.spec"),
        # Rebuild instead of reusing a stale build/ directory, and never
        # prompt, so the build is reproducible in CI and locally.
        "--noconfirm",
        "--clean",
    ]
    subprocess.run(command, cwd=project_root, check=True)


if __name__ == "__main__":
    main()

"""Build the standalone PyInstaller executable.

This mirrors the flags used by the release workflow in
``.github/workflows/release.yml`` so a local ``uv run build-sl`` produces a
binary equivalent to the published one. Keep the two in sync.
"""

import glob
import os
import subprocess
import sys
from pathlib import Path


def _tessdata_dir() -> str | None:
    """Return the directory holding tesseract's language data.

    Arch installs it at ``/usr/share/tessdata``; Debian and Ubuntu use
    ``/usr/share/tesseract-ocr/<major>/tessdata``. Assuming the Arch path is
    the only one is what broke the first OCR release build.
    """
    candidates = ["/usr/share/tessdata"]
    candidates += sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata"))
    for candidate in candidates:
        if Path(candidate).is_dir():
            return candidate
    return None


def _ocr_binary_args() -> list[str]:
    """Return the PyInstaller arguments that bundle tesseract and poppler."""
    if os.name == "nt":
        # release.yml collects the Windows tools; see "Collect OCR tools" there.
        print(
            "[build] WARNING: OCR tools are not bundled on Windows by this script",
            file=sys.stderr,
        )
        return []

    # These are not Python packages, so we use --add-binary
    args = []
    for binary in ("tesseract", "pdftoppm", "pdfinfo"):
        args += ["--add-binary", f"/usr/bin/{binary}:."]

    # tessdata directory (tesseract language data)
    tessdata_dir = _tessdata_dir()
    if tessdata_dir is None:
        print(
            "[build] WARNING: tessdata not found; OCR language data will be missing",
            file=sys.stderr,
        )
    else:
        args += ["--add-data", f"{tessdata_dir}:tessdata"]
    return args


def main():
    project_root = Path(__file__).parent.parent.parent.parent

    command = [
        sys.executable, "-m", "PyInstaller",
        "--name", "system-sl",
        "--windowed",
        "--noconsole",
        "--onefile",
        # Rebuild instead of reusing a stale build/ directory, and never
        # prompt, so the build is reproducible in CI and locally.
        "--noconfirm",
        "--clean",
        "--collect-data", "spellchecker",
        "--collect-all", "en_core_web_md",
        # AppImage bundles often lack a system CA bundle, which caused
        # SSL CERTIFICATE_VERIFY_FAILED when fetching models. Ship
        # certifi so urllib/requests can verify HTTPS certificates.
        "--collect-all", "certifi",
        *_ocr_binary_args(),
        # Python modules for OCR
        "--hidden-import", "pytesseract",
        "--hidden-import", "pdf2image",
        "--hidden-import", "pdf2image.pdf2image",
        str(project_root / "src/system_sl/frontend/gui/main.py"),
    ]

    subprocess.run(command, cwd=project_root, check=True)


if __name__ == "__main__":
    main()

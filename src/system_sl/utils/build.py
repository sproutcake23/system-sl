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
    
    # System binaries for OCR support (tesseract + poppler)
    # These are not Python packages, so we use --add-binary
    tesseract_bin = "/usr/bin/tesseract"
    pdftoppm_bin = "/usr/bin/pdftoppm"
    pdfinfo_bin = "/usr/bin/pdfinfo"
    
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
            "--collect-data", "spellchecker",
            "--collect-all", "en_core_web_md",
            # AppImage bundles often lack a system CA bundle, which caused
            # SSL CERTIFICATE_VERIFY_FAILED when fetching models. Ship
            # certifi so urllib/requests can verify HTTPS certificates.
            "--collect-all", "certifi",
            # OCR support: system binaries (tesseract + poppler utilities)
            "--add-binary", f"{tesseract_bin}:.",
            "--add-binary", f"{pdftoppm_bin}:.",
            "--add-binary", f"{pdfinfo_bin}:.",
            # tessdata directory (tesseract language data)
            "--add-data", "/usr/share/tessdata:tessdata",
            # Python modules for OCR
            "--hidden-import", "pytesseract",
            "--hidden-import", "pdf2image",
            "--hidden-import", "pdf2image.pdf2image",
            str(project_root / "src/system_sl/frontend/gui/main.py"),
        ],
        cwd=project_root,
        check=True,
    )


if __name__ == "__main__":
    main()
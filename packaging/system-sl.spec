# PyInstaller build for every platform: release.yml and build-sl both use this.
#
#   pyinstaller packaging/system-sl.spec --noconfirm --clean

import glob
import os
import re
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

ROOT = Path(SPECPATH).parent
IS_WINDOWS = os.name == "nt"

# Parts of Qt the app never uses. Leave Qt6Quick and Qt6Qml in: the app has no
# QML, but the FFmpeg media plugin links them, and sounds fail without them.
UNUSED_FILES = re.compile(
    r"""(^|/)(lib)?qt6(pdf|datavisualization|virtualkeyboard)[^/]*$
    | (^|/)qt(datavisualization|opengl|openglwidgets)\.
    | /plugins/platforminputcontexts/[^/]*virtualkeyboard
    | /plugins/imageformats/[^/]*qpdf
    | (^|/)pyside6/(qt/)?(qml|translations)/
    """,
    re.IGNORECASE | re.VERBOSE,
)

# The app only calls the Google Tasks API, whose description is this one file.
GOOGLE_DOCUMENTS = "googleapiclient/discovery_cache/documents/"
GOOGLE_DOCUMENT_KEPT = GOOGLE_DOCUMENTS + "tasks.v1.json"


def tessdata_dir():
    """Arch uses /usr/share/tessdata; Debian keeps it under tesseract-ocr/<major>."""
    candidates = ["/usr/share/tessdata", *sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata"))]
    return next((path for path in candidates if Path(path).is_dir()), None)


def is_unwanted(entry):
    name = entry[0].replace("\\", "/")
    if name.startswith(GOOGLE_DOCUMENTS):
        return name != GOOGLE_DOCUMENT_KEPT
    return bool(UNUSED_FILES.search(name))


def windows_ocr_files():
    """Return the folders release.yml collected, as files to bundle untouched.

    These must not go through Analysis. It scans any DLL it is given and copies
    the dependencies to the top level, which duplicated about 57 MiB of them.
    """
    bundle = ROOT / "ocr-bundle"
    if not bundle.is_dir():
        print("[build] WARNING: no ocr-bundle folder; OCR will not work", file=sys.stderr)
        return []
    return [
        (str(Path("ocr") / path.relative_to(bundle)), str(path), "DATA")
        for path in sorted(bundle.rglob("*"))
        if path.is_file()
    ]


datas = [(str(ROOT / "src/system_sl/assets"), "assets")]
binaries = []
hiddenimports = ["pytesseract", "pdf2image", "pdf2image.pdf2image"]

datas += collect_data_files("spellchecker")
for package in ("en_core_web_md", "certifi"):
    package_datas, package_binaries, package_imports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_imports

if not IS_WINDOWS:
    # On Linux, Analysis should scan these: it collects the libraries they need.
    binaries += [(f"/usr/bin/{tool}", ".") for tool in ("tesseract", "pdftoppm", "pdfinfo")]
    tessdata = tessdata_dir()
    if tessdata is None:
        print("[build] WARNING: tessdata not found; OCR will not work", file=sys.stderr)
    else:
        datas.append((tessdata, "tessdata"))

a = Analysis(
    [str(ROOT / "src/system_sl/frontend/gui/main.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
)

a.binaries = [entry for entry in a.binaries if not is_unwanted(entry)]
a.datas = [entry for entry in a.datas if not is_unwanted(entry)]
if IS_WINDOWS:
    a.datas += windows_ocr_files()

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="system-sl",
    console=False,
    upx=False,
)

#!/usr/bin/env python3
"""Build the app as a PyInstaller folder and package it as an AppImage.

An AppImage is already one file, and it reads its contents on demand. Putting a
one-file build inside it would unpack everything to the temp folder on every
launch for no benefit, so this makes its own one-folder build.

Do not install the project wheel into an AppDir: pip-generated console scripts
use an absolute shebang pointing to the build virtual environment.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Lets AppImageUpdate and similar tools find newer releases. appimagetool
# writes the matching .zsync file next to the AppImage; publish both.
UPDATE_INFORMATION = (
    "gh-releases-zsync|sproutcake23|system-sl|latest|system-sl-*-x86_64.AppImage.zsync"
)


def run_cmd(cmd, *, cwd=None, env=None):
    print(f"Running: {' '.join(map(str, cmd))}")
    subprocess.run(cmd, cwd=cwd, env=env, check=True)


def find_appimagetool(tools_dir: Path) -> Path:
    """Use a system appimagetool when available, otherwise download one."""
    installed = shutil.which("appimagetool")
    if installed:
        return Path(installed)

    tools_dir.mkdir()
    tool = tools_dir / "appimagetool"
    run_cmd([
        "wget", "-q", "-O", str(tool),
        "https://github.com/AppImage/appimagetool/releases/download/continuous/"
        "appimagetool-x86_64.AppImage",
    ])
    tool.chmod(0o755)
    return tool


def read_version(project_root: Path) -> str:
    """Return the project version.

    pyproject.toml declares the version dynamically from
    ``system_sl.__version__``, so it cannot be read from the table. Parse the
    attribute statically rather than importing the package, which would pull
    in the whole dependency tree.
    """
    init_module = project_root / "src" / "system_sl" / "__init__.py"
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']',
        init_module.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        raise SystemExit(f"Could not find __version__ in {init_module}")
    return match.group(1)


def build_bundle(project_root: Path) -> Path:
    """Build the one-folder bundle and return its directory."""
    dist = project_root / "dist" / "appimage"
    run_cmd(
        [
            sys.executable, "-m", "PyInstaller",
            project_root / "packaging" / "system-sl.spec",
            "--noconfirm", "--clean",
            "--distpath", dist,
            "--workpath", project_root / "build" / "appimage",
        ],
        cwd=project_root,
        env={**os.environ, "SYSTEM_SL_ONEDIR": "1"},
    )
    bundle = dist / "system-sl"
    if not os.access(bundle / "system-sl", os.X_OK):
        raise SystemExit(f"PyInstaller did not produce {bundle / 'system-sl'}.")
    return bundle


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    version = read_version(project_root)

    bundle = build_bundle(project_root)

    assets = project_root / "src" / "system_sl" / "assets"
    desktop = assets / "system-sl-appimage.desktop"
    icon = assets / "system-sl.svg"
    icon_png = assets / "system-sl.png"
    if not desktop.is_file() or not icon.is_file() or not icon_png.is_file():
        raise SystemExit("AppImage desktop file or icon is missing from src/system_sl/assets.")

    output = project_root / "dist" / f"system-sl-{version}-x86_64.AppImage"
    output.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="system-sl-appimage-") as temporary:
        temporary_dir = Path(temporary)
        appdir = temporary_dir / "system-sl.AppDir"
        app_lib_dir = appdir / "usr" / "lib"
        app_lib_dir.mkdir(parents=True)

        # Copy the bundle, not the Python packaging entry point. The former is
        # portable; the latter embeds the builder's virtualenv in its shebang.
        shutil.copytree(bundle, app_lib_dir / "system-sl", symlinks=True)
        shutil.copy2(desktop, appdir / "system-sl.desktop")
        shutil.copy2(icon, appdir / "system-sl.svg")
        shutil.copy2(icon_png, appdir / "system-sl.png")
        # appimagetool used to derive .DirIcon from the desktop file's icon;
        # since its 2026-10-29 continuous build it requires one up front, and
        # it must be a PNG: an SVG is flagged as invalid.
        (appdir / ".DirIcon").symlink_to("system-sl.png")

        apprun = appdir / "AppRun"
        apprun.write_text(
            "#!/bin/sh\n"
            "exec \"${APPDIR}/usr/lib/system-sl/system-sl\" \"$@\"\n"
        )
        apprun.chmod(0o755)

        appimagetool = find_appimagetool(temporary_dir / "tools")
        env = os.environ.copy()
        env["VERSION"] = version
        # appimagetool is itself an AppImage. Extract-and-run works on CI and
        # developer machines even when FUSE 2 is unavailable.
        env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
        # appimagetool writes the .zsync file into its working directory, so
        # run it from dist/ to keep the .zsync next to the AppImage.
        run_cmd(
            [appimagetool, "-u", UPDATE_INFORMATION, appdir, output],
            cwd=output.parent,
            env=env,
        )

    if not output.is_file() or not os.access(output, os.X_OK):
        raise SystemExit("appimagetool did not produce an executable AppImage.")
    if not output.with_name(output.name + ".zsync").is_file():
        raise SystemExit("appimagetool did not produce the .zsync file.")
    print(f"Created {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()

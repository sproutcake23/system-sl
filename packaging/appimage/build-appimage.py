#!/usr/bin/env python3
"""Create an AppImage from the standalone PyInstaller executable.

The PyInstaller executable is already self-contained. Do not install the
project wheel into an AppDir: pip-generated console scripts use an absolute
shebang pointing to the build virtual environment.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


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


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    version = read_version(project_root)

    binary = project_root / "dist" / "system-sl"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise SystemExit(
            "Missing executable dist/system-sl. Build it with PyInstaller before "
            "creating the AppImage."
        )

    assets = project_root / "src" / "system_sl" / "assets"
    desktop = assets / "system-sl-appimage.desktop"
    icon = assets / "system-sl.svg"
    if not desktop.is_file() or not icon.is_file():
        raise SystemExit("AppImage desktop file or icon is missing from src/system_sl/assets.")

    output = project_root / "dist" / f"system-sl-{version}-x86_64.AppImage"
    output.unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="system-sl-appimage-") as temporary:
        temporary_dir = Path(temporary)
        appdir = temporary_dir / "system-sl.AppDir"
        app_bin_dir = appdir / "usr" / "bin"
        app_bin_dir.mkdir(parents=True)

        # Copy the binary, not the Python packaging entry point. The former is
        # portable; the latter embeds the builder's virtualenv in its shebang.
        shutil.copy2(binary, app_bin_dir / "system-sl")
        shutil.copy2(desktop, appdir / "system-sl.desktop")
        shutil.copy2(icon, appdir / "system-sl.svg")

        apprun = appdir / "AppRun"
        apprun.write_text(
            "#!/bin/sh\n"
            "exec \"${APPDIR}/usr/bin/system-sl\" \"$@\"\n"
        )
        apprun.chmod(0o755)

        appimagetool = find_appimagetool(temporary_dir / "tools")
        env = os.environ.copy()
        env["VERSION"] = version
        # appimagetool is itself an AppImage. Extract-and-run works on CI and
        # developer machines even when FUSE 2 is unavailable.
        env.setdefault("APPIMAGE_EXTRACT_AND_RUN", "1")
        run_cmd([appimagetool, appdir, output], env=env)

    if not output.is_file() or not os.access(output, os.X_OK):
        raise SystemExit("appimagetool did not produce an executable AppImage.")
    print(f"Created {output} ({output.stat().st_size / 1024 / 1024:.1f} MiB)")


if __name__ == "__main__":
    main()

# Building and Releasing

## Building a standalone executable

Use the provided build script:

```bash
uv run build-sl
```

This runs PyInstaller with `packaging/system-sl.spec`, which the release workflow also uses, to produce a single one-file executable named `system-sl` in `dist/`:

- `--windowed --noconsole` — no terminal window on launch
- Bundles `assets/`, the spaCy model, and the spellchecker dictionary
- On Linux, bundles the `tesseract` and `poppler` binaries from `/usr/bin`, so install them first. On Windows, this script skips OCR; the release workflow collects those tools itself
- Leaves out parts of Qt and the Google API library that the app doesn't use
- Entry point: `system_sl/frontend/gui/main.py`

The result is a self-contained binary for your current OS.

To build the AppImage on Linux, which makes its own one-folder build so that it doesn't unpack on every launch:

```bash
uv run python packaging/appimage/build-appimage.py
```

## Preparing a release

Releases are built by GitHub Actions (`.github/workflows/release.yml`), not by hand.

1. Bump `__version__` in `src/system_sl/__init__.py` in a `chore: bump version to X.Y.Z` pull request. `pyproject.toml` reads the version from there.
2. After it merges, push a `vX.Y.Z` tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

The tag triggers the workflow, which builds on Linux and Windows and publishes a GitHub release containing:

- `system-sl-X.Y.Z-x86_64.AppImage` and its `.zsync` file
- `system-sl-linux.tar.xz`
- `system-sl-setup.exe`
- `system-sl-windows.zip`
- `SHA256SUMS.txt`

Users with **Auto Update** on receive it at their next launch.

Pull requests that change the build also run the workflow, without publishing anything. Both platforms must pass two checks in the built binary: `--check-ocr` reads a generated scanned PDF using only the bundled tools, and `--check-gui` opens the main window, loads a sound, and confirms the Google Tasks API description is present.

## Google OAuth client

Release builds bundle the app's Google OAuth client as
`assets/google_oauth_client.json`. The release workflow writes it from the
`GOOGLE_OAUTH_CLIENT` repository secret and fails if the secret is missing or
is not a Desktop app client. Set or rotate it with:

```bash
gh secret set GOOGLE_OAUTH_CLIENT < path/to/client.json
```

The file is gitignored and must never be committed. For a local build that
includes it, copy your client to `src/system_sl/assets/google_oauth_client.json`
before running `uv run build-sl`.

## Development setup

```bash
uv sync
uv run system-sl
```

Run the project from source during development; use the built executable only for release artifacts.

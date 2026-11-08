# Building and Releasing

## Building a standalone executable

Use the provided build script:

```bash
uv run build-sl
```

This invokes PyInstaller to produce a single one-file executable named `system-sl` in `dist/`:

- `--windowed --noconsole` — no terminal window on launch
- Bundles `assets/`, the spaCy model, and the spellchecker dictionary
- Bundles the `tesseract` and `poppler` binaries from `/usr/bin`, so install them on the build machine first. The script therefore expects Linux
- Entry point: `system_sl/frontend/gui/main.py`

The result is a self-contained binary for your current OS.

To package that binary as an AppImage on Linux:

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

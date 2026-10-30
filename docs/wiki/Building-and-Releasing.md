# Building and Releasing

## Building a standalone executable

Use the provided build script:

```bash
uv run build-sl
```

This invokes PyInstaller to produce a single one-file executable named `system-sl` in `dist/`:

- `--windowed --noconsole` — no terminal window on launch
- Bundles `assets/` and the spellchecker dictionary
- Entry point: `system_sl/frontend/gui/main.py`

The result is a self-contained binary for your current OS. Build once per platform (Linux and Windows) to produce the release artifacts.

## Preparing a release

1. Bump the version in `pyproject.toml`.
2. Build for each target platform.
3. Package each executable into a zip:
   - `system-sl-linux.zip`
   - `system-sl-windows.zip`
4. Create a [GitHub Release](https://github.com/sproutcake23/System-SL/releases/new) and attach the zips.

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

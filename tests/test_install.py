"""Desktop-entry generation and the development project root."""

from pathlib import Path

from system_sl.utils import install


def test_desktop_entry_quotes_paths_with_spaces(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    target = Path("/opt/my apps/system-sl")
    install_dir = Path("/opt/my apps")

    install.create_desktop_shortcut(target, install_dir)

    entry = tmp_path / ".local" / "share" / "applications" / "system-sl.desktop"
    text = entry.read_text()
    assert 'Exec="/opt/my apps/system-sl"' in text
    # Path is not an Exec-style key: quotes would become part of the name.
    assert "Path=/opt/my apps\n" in text


def test_development_project_root_is_the_repo_root():
    # utils -> system_sl -> src -> repository root
    root = Path(install.__file__).parent.parent.parent.parent
    assert (root / "pyproject.toml").exists()
    assert (root / "src" / "system_sl").is_dir()
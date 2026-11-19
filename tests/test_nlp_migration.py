"""The retired spaCy packages are removed from upgraded pip/editable installs."""

import subprocess

from system_sl.utils import nlp_migration


def test_nothing_to_do_when_no_legacy_packages(monkeypatch):
    monkeypatch.setattr(nlp_migration, "legacy_packages_installed", lambda: [])
    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(subprocess, "run", fail)
    nlp_migration.cleanup_legacy_nlp()
    assert called is False


def test_frozen_builds_are_skipped(monkeypatch):
    monkeypatch.setattr(nlp_migration, "legacy_packages_installed", lambda: ["spacy"])
    monkeypatch.setattr(nlp_migration.sys, "frozen", True, raising=False)
    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(subprocess, "run", fail)
    nlp_migration.cleanup_legacy_nlp()
    assert called is False


def test_appimage_is_skipped(monkeypatch):
    monkeypatch.setattr(nlp_migration, "legacy_packages_installed", lambda: ["spacy"])
    monkeypatch.setenv("APPIMAGE", "/tmp/system-sl.AppImage")
    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(subprocess, "run", fail)
    nlp_migration.cleanup_legacy_nlp()
    assert called is False


def test_system_python_is_never_modified(monkeypatch):
    monkeypatch.setattr(
        nlp_migration, "legacy_packages_installed", lambda: ["spacy", "en_core_web_md"]
    )
    monkeypatch.setattr(nlp_migration.sys, "prefix", "/usr")
    monkeypatch.setattr(nlp_migration.sys, "base_prefix", "/usr")
    monkeypatch.delenv("APPIMAGE", raising=False)
    called = False

    def fail(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("system Python must not be modified")

    monkeypatch.setattr(subprocess, "run", fail)
    nlp_migration.cleanup_legacy_nlp()
    assert called is False


def test_virtualenv_uninstalls_legacy_packages(monkeypatch):
    monkeypatch.setattr(
        nlp_migration, "legacy_packages_installed", lambda: ["spacy", "en_core_web_md"]
    )
    monkeypatch.setattr(nlp_migration.sys, "prefix", "/repo/.venv")
    monkeypatch.setattr(nlp_migration.sys, "base_prefix", "/usr")
    monkeypatch.delenv("APPIMAGE", raising=False)

    recorded = {}

    def fake_run(command, **kwargs):
        recorded["command"] = command
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    nlp_migration.cleanup_legacy_nlp()

    command = recorded["command"]
    assert command[0] == nlp_migration.sys.executable
    assert command[1:4] == ["-m", "pip", "uninstall"]
    assert "-y" in command
    assert "spacy" in command
    assert "en_core_web_md" in command


def test_pip_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(nlp_migration, "legacy_packages_installed", lambda: ["spacy"])
    monkeypatch.setattr(nlp_migration.sys, "prefix", "/repo/.venv")
    monkeypatch.setattr(nlp_migration.sys, "base_prefix", "/usr")
    monkeypatch.delenv("APPIMAGE", raising=False)

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="no pip")

    monkeypatch.setattr(subprocess, "run", fake_run)
    nlp_migration.cleanup_legacy_nlp()  # must not raise


def test_missing_pip_does_not_raise(monkeypatch):
    monkeypatch.setattr(nlp_migration, "legacy_packages_installed", lambda: ["spacy"])
    monkeypatch.setattr(nlp_migration.sys, "prefix", "/repo/.venv")
    monkeypatch.setattr(nlp_migration.sys, "base_prefix", "/usr")
    monkeypatch.delenv("APPIMAGE", raising=False)

    def fake_run(*args, **kwargs):
        raise FileNotFoundError("python -m pip")

    monkeypatch.setattr(subprocess, "run", fake_run)
    nlp_migration.cleanup_legacy_nlp()  # must not raise
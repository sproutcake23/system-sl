from system_sl.utils.autostart import CrossPlatformAutostart


def test_toggle_reports_a_failed_disable(monkeypatch):
    autostart = CrossPlatformAutostart("Test")
    monkeypatch.setattr(autostart, "is_enabled", lambda: True)
    monkeypatch.setattr(autostart, "disable", lambda: False)
    assert autostart.toggle() is True

    monkeypatch.setattr(autostart, "disable", lambda: True)
    assert autostart.toggle() is False


def test_toggle_reports_the_enable_result(monkeypatch):
    autostart = CrossPlatformAutostart("Test")
    monkeypatch.setattr(autostart, "is_enabled", lambda: False)
    monkeypatch.setattr(autostart, "enable", lambda: True)
    assert autostart.toggle() is True
    monkeypatch.setattr(autostart, "enable", lambda: False)
    assert autostart.toggle() is False


def test_desktop_exec_quote_escapes_per_spec():
    from system_sl.utils.autostart import desktop_exec_quote

    assert desktop_exec_quote("/opt/my apps/sl") == '"/opt/my apps/sl"'
    assert desktop_exec_quote('/a"b$c`d\\e%f') == '"/a\\"b\\$c\\`d\\\\e%%f"'


def test_linux_entry_round_trips_and_path_is_unquoted(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("HOME", str(tmp_path))
    autostart = CrossPlatformAutostart("Test")
    monkeypatch.setattr(autostart, "os_type", "Linux")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    appimage = tmp_path / "my apps" / "System 50%.AppImage"
    appimage.parent.mkdir()
    appimage.write_text("")
    monkeypatch.setenv("APPIMAGE", str(appimage))

    assert autostart.enable() is True
    text = (tmp_path / ".config" / "autostart" / "test.desktop").read_text()
    assert "Path=" not in text  # frozen builds have no working directory
    assert "%%" in text
    assert autostart.recorded_executable() == appimage


def test_dev_entry_path_is_never_quoted(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv("HOME", str(tmp_path))
    repo = tmp_path / "dev dir"
    (repo / ".venv").mkdir(parents=True)
    script = repo / "run.py"
    script.write_text("")
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.delattr(sys, "frozen", raising=False)
    autostart = CrossPlatformAutostart("Test")
    monkeypatch.setattr(autostart, "os_type", "Linux")

    assert autostart.enable() is True
    text = (tmp_path / ".config" / "autostart" / "test.desktop").read_text()
    assert f"Path={repo}\n" in text

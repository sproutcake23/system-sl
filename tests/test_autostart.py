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

"""The translation helper falls back to the source string."""

from system_sl.utils.i18n import tr


def test_tr_returns_source_without_a_catalog():
    assert tr("Reset Settings") == "Reset Settings"
    assert tr("") == ""

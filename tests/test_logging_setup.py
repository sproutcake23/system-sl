"""A single rotating file log for both processes."""

import logging

from system_sl.utils import logging_setup


def test_configure_writes_to_the_config_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    logging_setup._configured = False
    logging_setup.configure_logging()
    logging.getLogger("system-sl.test").info("hello from the log")
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_file = tmp_path / ".config" / "system-sl" / "system-sl.log"
    assert log_file.exists()
    assert "hello from the log" in log_file.read_text()

"""Cancellation of a Google sign-in that is waiting on the browser."""

import threading

import pytest

from system_sl.core.sync_service import GoogleSyncEngine
from system_sl.utils.google_api import (
    GoogleAPIClient,
    GoogleSyncCancelled,
)


class _Provider:
    source_name = "task"

    def fetch_raw_data(self, *args, **kwargs):  # pragma: no cover - never reached
        raise AssertionError("cancelled sync must not fetch")


def test_engine_checks_the_cancel_event_before_network():
    event = threading.Event()
    event.set()
    engine = GoogleSyncEngine(api_client=object(), cancel_event=event)

    with pytest.raises(GoogleSyncCancelled):
        engine.execute_sync(_Provider())


def test_client_checks_the_cancel_event():
    event = threading.Event()
    client = GoogleAPIClient(scopes=[], cancel_event=event)
    client._check_cancelled()  # no-op while unset
    event.set()
    with pytest.raises(GoogleSyncCancelled):
        client._check_cancelled()
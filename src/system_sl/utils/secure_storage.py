"""Secure secret storage for System-SL.

Secrets (LLM API keys, OAuth access and refresh tokens) are stored
in the operating system's secret service via the :mod:`keyring` library, which
backends to:

* KDE Wallet / KDE Wallet Manager (KDE Plasma, via SecretService DBus)
* GNOME Keyring (GNOME, via SecretService DBus)
* macOS Keychain
* Windows Credential Manager

If no secret-service backend is available (headless, CI, containers), secrets
fall back to a per-machine encrypted file using Fernet symmetric encryption
with a key derived from the host machine-id. This keeps secrets off the
filesystem in plaintext while remaining functional everywhere.

.. warning::
   The fallback encryption key is derived from ``/etc/machine-id`` (or
   ``/var/lib/dbus/machine-id``), which is **world-readable on Linux**.
   Any local process can derive the same key and decrypt stored secrets.
   The fallback protects against *casual* filesystem reading, NOT against
   local attackers. For real security, ensure a secret service is running
   (gnome-keyring, kwallet, etc.) or set ``SYSTEM_SL_REQUIRE_KEYRING=1``
   to make the absence of a keyring a hard error.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging

logger = logging.getLogger(__name__)
import os
from pathlib import Path
from typing import Callable, Optional

import keyring
from cryptography.fernet import Fernet

from system_sl.utils.paths import get_tasks_file_path


# All system-sl secrets share one keyring service name so they are grouped
# together in the user's secret store.
SERVICE_NAME = "system-sl"

# Logical secret names.
SECRET_LLM_API_KEY = "llm-api-key"
SECRET_GOOGLE_TOKEN = "google-oauth-token"
SECRET_SETTINGS = "settings"

# Fallback encrypted store (used only when keyring has no usable backend).
_FALLBACK_FILE = Path(get_tasks_file_path(".secrets.enc"))

# Callback for GUI notification when fallback is activated (set by main.py).
_fallback_warning_callback: Optional[Callable[[], None]] = None
_fallback_warned = False


def register_fallback_warning_callback(callback: Callable[[], None]) -> None:
    """Register a callback to be invoked when the fallback store is used.

    The GUI (main.py) should call this at startup to show a user-visible
    warning (e.g., system tray toast).
    """
    global _fallback_warning_callback
    _fallback_warning_callback = callback


def _notify_fallback_used() -> None:
    """Log warning and invoke GUI callback (once per process)."""
    global _fallback_warned
    if _fallback_warned:
        return
    _fallback_warned = True

    msg = (
        "SECURITY WARNING: Storing secrets in encrypted-file fallback. "
        "The encryption key is derived from /etc/machine-id, which is "
        "readable by any local user. For real security, install a secret "
        "service (gnome-keyring, kwallet) or set SYSTEM_SL_REQUIRE_KEYRING=1."
    )
    logging.warning(msg)

    if _fallback_warning_callback:
        try:
            _fallback_warning_callback()
        except Exception:
            # Callback must not crash the storage layer.
            logger.debug("fallback warning callback failed", exc_info=True)


# ---------------------------------------------------------------------------
# Fallback encrypted store
# ---------------------------------------------------------------------------


def _fernet_key() -> bytes:
    """Derive a stable per-machine Fernet key from the host machine-id.

    .. warning::
       The machine-id is world-readable on Linux. This provides only
       obfuscation against casual inspection, not cryptographic security
       against local attackers.
    """
    machine_id: Optional[str] = None
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            if Path(candidate).is_file():
                machine_id = Path(candidate).read_text().strip()
                break
        except OSError:
            pass
    if not machine_id:
        machine_id = os.environ.get("SYSTEM_SL_MACHINE_ID", "system-sl-default")
    digest = hashlib.sha256(machine_id.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fallback_fernet() -> Fernet:
    return Fernet(_fernet_key())


def _fallback_get(name: str) -> Optional[str]:
    try:
        if not _FALLBACK_FILE.is_file():
            return None
        fernet = _fallback_fernet()
        blob = _FALLBACK_FILE.read_bytes()
        plaintext = fernet.decrypt(blob)
        data = json.loads(plaintext.decode("utf-8"))
        return data.get(name)
    except Exception:
        return None


def _fallback_set(name: str, value: str) -> None:
    _notify_fallback_used()
    try:
        fernet = _fallback_fernet()
        data: dict = {}
        if _FALLBACK_FILE.is_file():
            try:
                data = json.loads(
                    _fallback_fernet().decrypt(_FALLBACK_FILE.read_bytes()).decode("utf-8")
                )
            except Exception:
                logger.debug("could not read the encrypted fallback", exc_info=True)
                data = {}
        data[name] = value
        _FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FALLBACK_FILE.write_bytes(fernet.encrypt(json.dumps(data).encode("utf-8")))
        _FALLBACK_FILE.chmod(0o600)
    except Exception:
        logger.debug("could not write the encrypted fallback", exc_info=True)


def _fallback_delete(name: str) -> None:
    try:
        if not _FALLBACK_FILE.is_file():
            return
        _notify_fallback_used()
        fernet = _fallback_fernet()
        data = json.loads(fernet.decrypt(_FALLBACK_FILE.read_bytes()).decode("utf-8"))
        data.pop(name, None)
        _FALLBACK_FILE.write_bytes(fernet.encrypt(json.dumps(data).encode("utf-8")))
    except Exception:
        logger.debug("could not delete the encrypted fallback", exc_info=True)


# ---------------------------------------------------------------------------
# Keyring availability check
# ---------------------------------------------------------------------------


def _keyring_available() -> bool:
    """Return True if a keyring backend is actually usable.

    Unlike the naive check, this attempts a dummy write/read cycle to
    verify the backend works (some backends exist but fail at runtime,
    e.g., headless DBus without a session bus).
    """
    try:
        kr = keyring.get_keyring()
        if kr is None:
            return False
        # Test with a throwaway entry.
        test_service = f"{SERVICE_NAME}.probe"
        test_key = ".probe"
        test_value = "ok"
        kr.set_password(test_service, test_key, test_value)
        got = kr.get_password(test_service, test_key)
        try:
            kr.delete_password(test_service, test_key)
        except Exception:
            logger.debug("could not remove the keyring probe", exc_info=True)
        return got == test_value
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _require_keyring_or_fail() -> None:
    """Raise if SYSTEM_SL_REQUIRE_KEYRING=1 and no keyring backend works."""
    if os.environ.get("SYSTEM_SL_REQUIRE_KEYRING") == "1":
        if not _keyring_available():
            raise RuntimeError(
                "SYSTEM_SL_REQUIRE_KEYRING=1 but no usable keyring backend found. "
                "Install gnome-keyring / kwallet / secret-service, or run with a "
                "D-Bus session bus. Secrets will not be stored in the weak fallback."
            )


def set_secret(name: str, value: str) -> None:
    """Store ``value`` under the secret ``name``.

    Uses the system keyring when available, otherwise the encrypted-file
    fallback (with a warning).
    """
    if value is None:
        delete_secret(name)
        return
    _require_keyring_or_fail()
    try:
        if _keyring_available():
            keyring.set_password(SERVICE_NAME, name, value)
            return
    except Exception:
        logger.debug("keyring write failed; using the fallback", exc_info=True)
    _fallback_set(name, value)


def get_secret(name: str) -> Optional[str]:
    """Retrieve the secret ``name``, or ``None`` if not stored."""
    try:
        if _keyring_available():
            value = keyring.get_password(SERVICE_NAME, name)
            if value is not None:
                return value
    except Exception:
        logger.debug("keyring read failed; using the fallback", exc_info=True)
    return _fallback_get(name)


def delete_secret(name: str) -> None:
    """Remove the secret ``name`` from whichever store holds it."""
    try:
        if _keyring_available():
            try:
                keyring.delete_password(SERVICE_NAME, name)
            except keyring.errors.PasswordDeleteError:
                logger.debug("secret %s was not in the keyring", name)
    except Exception:
        logger.debug("keyring delete failed; using the fallback", exc_info=True)
    # Always clear the encrypted fallback too: get_secret falls back to it
    # when the keyring has nothing, so a copy left there would come back.
    _fallback_delete(name)


def set_json_secret(name: str, data: dict) -> None:
    """Store a JSON-serialisable dict as a secret under ``name``."""
    set_secret(name, json.dumps(data))


def get_json_secret(name: str) -> Optional[dict]:
    """Retrieve a JSON secret under ``name``, or ``None`` if not stored."""
    raw = get_secret(name)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
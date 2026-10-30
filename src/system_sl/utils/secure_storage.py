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
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# Fallback encrypted store
# ---------------------------------------------------------------------------

def _fernet_key() -> bytes:
    """Derive a stable per-machine Fernet key from the host machine-id."""
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
    try:
        fernet = _fallback_fernet()
        data: dict = {}
        if _FALLBACK_FILE.is_file():
            try:
                data = json.loads(_fallback_fernet().decrypt(_FALLBACK_FILE.read_bytes()).decode("utf-8"))
            except Exception:
                data = {}
        data[name] = value
        _FALLBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        _FALLBACK_FILE.write_bytes(fernet.encrypt(json.dumps(data).encode("utf-8")))
        _FALLBACK_FILE.chmod(0o600)
    except Exception:
        pass


def _fallback_delete(name: str) -> None:
    try:
        if not _FALLBACK_FILE.is_file():
            return
        fernet = _fallback_fernet()
        data = json.loads(fernet.decrypt(_FALLBACK_FILE.read_bytes()).decode("utf-8"))
        data.pop(name, None)
        _FALLBACK_FILE.write_bytes(fernet.encrypt(json.dumps(data).encode("utf-8")))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _keyring_available() -> bool:
    """Return True if a keyring backend is actually usable."""
    try:
        return keyring.get_keyring() is not None
    except Exception:
        return False


def set_secret(name: str, value: str) -> None:
    """Store ``value`` under the secret ``name``.

    Uses the system keyring when available, otherwise the encrypted-file
    fallback.
    """
    if value is None:
        delete_secret(name)
        return
    try:
        if _keyring_available():
            keyring.set_password(SERVICE_NAME, name, value)
            return
    except Exception:
        pass
    _fallback_set(name, value)


def get_secret(name: str) -> Optional[str]:
    """Retrieve the secret ``name``, or ``None`` if not stored."""
    try:
        if _keyring_available():
            value = keyring.get_password(SERVICE_NAME, name)
            if value is not None:
                return value
    except Exception:
        pass
    return _fallback_get(name)


def delete_secret(name: str) -> None:
    """Remove the secret ``name`` from whichever store holds it."""
    try:
        if _keyring_available():
            try:
                keyring.delete_password(SERVICE_NAME, name)
            except keyring.errors.PasswordDeleteError:
                pass
            return
    except Exception:
        pass
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

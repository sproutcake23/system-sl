"""Updates are installed only when the checksums carry the release key's signature."""

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import system_sl.utils.release_signing as rs
from system_sl.utils.auto_updater import find_signature_asset, signature_failure

CHECKSUMS = b"0123abcd  system-sl-windows.zip\n"


def _keypair():
    private = Ed25519PrivateKey.generate()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    return private, public_pem


def test_valid_signature_passes():
    private, public_pem = _keypair()
    rs.verify_checksums_signature(CHECKSUMS, private.sign(CHECKSUMS), public_pem)


def test_tampered_checksums_are_rejected():
    private, public_pem = _keypair()
    signature = private.sign(CHECKSUMS)
    with pytest.raises(rs.SignatureError):
        rs.verify_checksums_signature(CHECKSUMS.replace(b"0123", b"9999"), signature, public_pem)


def test_signature_from_another_key_is_rejected():
    _, public_pem = _keypair()
    other, _ = _keypair()
    with pytest.raises(rs.SignatureError):
        rs.verify_checksums_signature(CHECKSUMS, other.sign(CHECKSUMS), public_pem)


@pytest.mark.parametrize("signature", [None, b""])
def test_missing_signature_fails_closed(signature):
    _, public_pem = _keypair()
    with pytest.raises(rs.SignatureError, match="no signature"):
        rs.verify_checksums_signature(CHECKSUMS, signature, public_pem)


def test_a_build_without_a_key_refuses_updates():
    private, _ = _keypair()
    with pytest.raises(rs.SignatureError, match="no release signing key"):
        rs.verify_checksums_signature(CHECKSUMS, private.sign(CHECKSUMS), "")


def test_updater_reports_signature_problems(monkeypatch):
    private, public_pem = _keypair()
    monkeypatch.setattr(rs, "RELEASE_PUBLIC_KEY_PEM", public_pem)

    assert signature_failure(CHECKSUMS, private.sign(CHECKSUMS)) is None
    failure = signature_failure(CHECKSUMS, None)
    assert failure.startswith("Update failed:") and "Refusing to install" in failure


def test_signature_asset_is_found_next_to_the_checksums():
    checksums = {"name": "SHA256SUMS.txt", "browser_download_url": "u1"}
    signature = {"name": "SHA256SUMS.txt.sig", "browser_download_url": "u2"}
    assets = [{"name": "system-sl-windows.zip"}, checksums, signature]

    assert find_signature_asset(assets, checksums) is signature
    assert find_signature_asset(assets[:2], checksums) is None
    assert find_signature_asset(assets, None) is None

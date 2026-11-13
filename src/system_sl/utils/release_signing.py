"""Verify that a release's checksum file was signed by the project's key.

The release workflow signs ``SHA256SUMS.txt`` with an Ed25519 private key held
only in the ``RELEASE_SIGNING_KEY`` repository secret, and publishes the raw
64-byte signature as ``SHA256SUMS.txt.sig``. The updater checks that signature
against the public key below before it trusts any checksum in the file.

A checksum alone only proves the download matches the release's own metadata,
which anyone able to publish a release could rewrite together. The signature
ties the checksums to a key that never leaves the maintainer's control.

To create or rotate the key, see "Release signing" in
docs/wiki/Building-and-Releasing.md.
"""

from __future__ import annotations

# Ed25519 public key (PEM) matching the RELEASE_SIGNING_KEY secret. Updates
# are refused if this is empty.
RELEASE_PUBLIC_KEY_PEM = """\
-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAwKhFUM9WKBAtPXGVz9kWYDgreB3Cj4D1PQvQ3RiOaVI=
-----END PUBLIC KEY-----
"""

SIGNATURE_SUFFIX = ".sig"


class SignatureError(Exception):
    """The checksums could not be shown to come from the project's key."""


def signature_asset_name(checksums_name: str) -> str:
    """Return the release asset name that holds the signature."""
    return checksums_name + SIGNATURE_SUFFIX


def verify_checksums_signature(
    checksums: bytes,
    signature: bytes | None,
    public_key_pem: str | None = None,
) -> None:
    """Raise :class:`SignatureError` unless ``signature`` signs ``checksums``.

    Fails closed: a missing key, a missing signature or a bad one all raise.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    pem = RELEASE_PUBLIC_KEY_PEM if public_key_pem is None else public_key_pem
    if not pem.strip():
        raise SignatureError(
            "this build has no release signing key, so updates can't be verified."
        )
    if not signature:
        raise SignatureError("this release publishes no signature for its checksums.")
    try:
        key = load_pem_public_key(pem.encode("ascii"))
    except ValueError as exc:
        raise SignatureError(f"the built-in release key is invalid: {exc}") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise SignatureError("the built-in release key is not an Ed25519 key.")
    try:
        key.verify(signature, checksums)
    except InvalidSignature as exc:
        raise SignatureError(
            "the checksums signature does not match the release key."
        ) from exc

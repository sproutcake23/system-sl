"""Auto-updater safety: archive extraction and fail-closed checksums."""

import hashlib
import stat
import zipfile

import pytest

from system_sl.utils.auto_updater import _safe_extract_zip, checksum_failure


def _write_zip(tmp_path, member: str, payload: bytes = b"payload") -> zipfile.ZipFile:
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(member, payload)
    return zipfile.ZipFile(path, "r")


def test_zip_slip_parent_path_is_rejected(tmp_path):
    archive = _write_zip(tmp_path, "../escaped.txt")
    dest = tmp_path / "dest"
    dest.mkdir()

    with archive:
        with pytest.raises(ValueError):
            _safe_extract_zip(archive, dest)

    assert not (tmp_path / "escaped.txt").exists()


def test_zip_slip_absolute_path_is_rejected(tmp_path):
    archive = _write_zip(tmp_path, "/tmp/escaped.txt")
    dest = tmp_path / "dest"
    dest.mkdir()

    with archive:
        with pytest.raises(ValueError):
            _safe_extract_zip(archive, dest)


def test_zip_slip_symlink_is_rejected(tmp_path):
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")

    dest = tmp_path / "dest"
    dest.mkdir()
    with zipfile.ZipFile(path, "r") as archive:
        with pytest.raises(ValueError):
            _safe_extract_zip(archive, dest)

    assert not (dest / "link").exists()


def test_normal_member_is_extracted(tmp_path):
    archive = _write_zip(tmp_path, "folder/system-sl", b"binary")
    dest = tmp_path / "dest"
    dest.mkdir()

    with archive:
        _safe_extract_zip(archive, dest)

    assert (dest / "folder" / "system-sl").read_bytes() == b"binary"


def test_missing_checksums_asset_fails_closed(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"download")

    failure = checksum_failure(archive, "asset.zip", None)

    assert failure is not None
    assert "no checksums" in failure
    assert "Refusing" in failure


def test_asset_without_checksum_entry_fails_closed(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"download")

    failure = checksum_failure(archive, "asset.zip", "deadbeef  other.zip\n")

    assert failure is not None
    assert "no checksum entry" in failure


def test_matching_checksum_passes(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"download")
    digest = hashlib.sha256(b"download").hexdigest()

    assert checksum_failure(archive, "asset.zip", f"{digest}  asset.zip\n") is None


def test_mismatched_checksum_fails_closed(tmp_path):
    archive = tmp_path / "asset.zip"
    archive.write_bytes(b"download")
    wrong = hashlib.sha256(b"something else").hexdigest()

    failure = checksum_failure(archive, "asset.zip", f"{wrong}  asset.zip\n")

    assert failure is not None
    assert "mismatch" in failure

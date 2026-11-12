"""Auto-updater safety: archive extraction and fail-closed checksums."""

import hashlib
import io
import os
import stat
import tarfile
import zipfile

import pytest

from system_sl.utils.auto_updater import (
    _checked_tar_members,
    _safe_extract_tar,
    _safe_extract_zip,
    checksum_failure,
    extract_binary,
)


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


# --- tar extraction -----------------------------------------------------------


def _member(name, kind=tarfile.REGTYPE, payload=b"payload", linkname="", mode=0o644):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.mode = mode
    info.linkname = linkname
    data = None
    if kind == tarfile.REGTYPE:
        info.size = len(payload)
        data = io.BytesIO(payload)
    return info, data


def _write_tar(tmp_path, *members):
    path = tmp_path / "update.tar.xz"
    with tarfile.open(path, "w:xz") as tf:
        for info, data in members:
            tf.addfile(info, data)
    return path


@pytest.fixture(params=["filter", "manual"])
def tar_mode(request, monkeypatch):
    """Run each tar test with tarfile's data filter and with the manual checks."""
    if request.param == "manual":
        monkeypatch.delattr(tarfile, "data_filter", raising=False)
    elif not hasattr(tarfile, "data_filter"):
        pytest.skip("tarfile data filter unavailable on this interpreter")
    return request.param


# Factories: each member carries a one-shot payload stream.
_MALICIOUS = {
    "parent": lambda: [_member("../escaped.txt")],
    "absolute": lambda: [_member("/tmp/escaped.txt")],
    "nested_parent": lambda: [_member("folder/../../escaped.txt")],
    "symlink_absolute": lambda: [_member("link", tarfile.SYMTYPE, linkname="/etc/passwd")],
    "symlink_parent": lambda: [_member("link", tarfile.SYMTYPE, linkname="../../outside")],
    "hardlink_parent": lambda: [_member("hard", tarfile.LNKTYPE, linkname="../outside")],
    "chardev": lambda: [_member("dev", tarfile.CHRTYPE)],
    "blockdev": lambda: [_member("blk", tarfile.BLKTYPE)],
    "fifo": lambda: [_member("pipe", tarfile.FIFOTYPE)],
}


@pytest.mark.parametrize("case", sorted(_MALICIOUS))
def test_tar_escape_is_rejected(tmp_path, tar_mode, case):
    path = _write_tar(tmp_path, *_MALICIOUS[case]())
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(path, "r:xz") as archive:
        if tar_mode == "filter" and case == "absolute":
            # The data filter strips the leading "/" and keeps it inside dest.
            _safe_extract_tar(archive, dest)
            assert (dest / "tmp" / "escaped.txt").is_file()
        else:
            with pytest.raises((ValueError, tarfile.TarError)):
                _safe_extract_tar(archive, dest)

    assert not (tmp_path / "escaped.txt").exists()
    assert not (tmp_path / "outside").exists()


def test_manual_check_rejects_before_writing_anything(tmp_path):
    path = _write_tar(
        tmp_path, _member("folder/system-sl", payload=b"ok"), _member("../escaped.txt")
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(path, "r:xz") as archive:
        with pytest.raises(ValueError):
            _checked_tar_members(archive, dest)
    assert list(dest.iterdir()) == []


def test_tar_normal_archive_is_extracted(tmp_path, tar_mode):
    path = _write_tar(
        tmp_path,
        _member("system-sl-linux", tarfile.DIRTYPE, mode=0o755),
        _member("system-sl-linux/system-sl", payload=b"binary", mode=0o755),
        _member("system-sl-linux/current", tarfile.SYMTYPE, linkname="system-sl"),
    )
    dest = tmp_path / "dest"
    dest.mkdir()

    binary = extract_binary(path, dest)

    assert binary == dest / "system-sl-linux" / "system-sl"
    assert binary.read_bytes() == b"binary"
    assert (dest / "system-sl-linux" / "current").resolve() == binary.resolve()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_manual_tar_clears_setuid(tmp_path, monkeypatch):
    monkeypatch.delattr(tarfile, "data_filter", raising=False)
    path = _write_tar(tmp_path, _member("system-sl", payload=b"x", mode=0o4755))
    dest = tmp_path / "dest"
    dest.mkdir()

    extract_binary(path, dest)

    assert not (dest / "system-sl").stat().st_mode & stat.S_ISUID

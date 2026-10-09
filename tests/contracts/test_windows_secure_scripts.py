"""Secure storage policy and harmless lifetime/substitution fault injection."""
from pathlib import Path
import ctypes
import pytest

from rekordbox_usb.windows_secure_scripts import (
    secure_diskpart_script, validate_security, SecureScriptError, WindowsSecureScriptIO,
)
from rekordbox_usb.source_write_guard import SourceWriteBoundary
from tests.fixtures.secure_script_io import BenignSecureIO


@pytest.fixture
def storage(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    return BenignSecureIO(tmp_path / "privileged-model" / "secure-runtime"), SourceWriteBoundary(source)


@pytest.mark.parametrize("failure", [None, "timeout", "spawn_failure"])
def test_exclusive_absolute_script_and_cleanup_for_all_outcomes(storage, failure):
    io, boundary = storage
    try:
        with secure_diskpart_script("inert fixture text", boundary, io=io) as path:
            assert path.is_absolute() and path.parent == io.location
            assert path.read_text() == "inert fixture text"
            with pytest.raises(FileExistsError):
                path.open("x")
            with pytest.raises(PermissionError):
                io.unprivileged_write(path)
            with pytest.raises(PermissionError):
                io.unprivileged_write(path.parent)
            if failure:
                raise RuntimeError(failure)
    except RuntimeError:
        assert failure is not None
    assert io.created == io.deleted
    assert not io.created[0].exists()
    assert set(io.handles) <= set(io.closed)


@pytest.mark.parametrize("target", ["root", "parent"])
def test_reparse_substitution_fails_before_script_creation(storage, target):
    io, boundary = storage
    io.reparse.add(io.location if target == "root" else io.location.parent)
    with pytest.raises(SecureScriptError, match="reparse"):
        with secure_diskpart_script("inert", boundary, io=io):
            pytest.fail("must not spawn")
    assert not io.created
    assert set(io.handles) <= set(io.closed)


def test_replaced_directory_identity_stops_before_consumer(storage):
    io, boundary = storage
    io.changed = io.location
    with pytest.raises(SecureScriptError, match="ancestor identity"):
        with secure_diskpart_script("inert", boundary, io=io):
            pytest.fail("must not spawn")
    assert io.created == io.deleted


def test_foreign_acl_fails_closed_without_fallback(storage):
    io, boundary = storage
    io.foreign_acl = True
    with pytest.raises(SecureScriptError, match="trustee"):
        with secure_diskpart_script("inert", boundary, io=io):
            pytest.fail("must not spawn")
    assert not io.created


@pytest.mark.parametrize("fault", ["owner", "inherited", "null", "users", "unsupported", "generic", "inheritance"])
def test_acl_policy_rejects_unprivileged_or_ambiguous_access(fault):
    owner, protected = "S-1-5-32-544", True
    entries = [(0, 0, "S-1-5-18", 0x1F01FF), (0, 0, "S-1-5-32-544", 0x1F01FF)]
    if fault == "owner": owner = "S-1-5-21-123-456-789-1001"
    if fault == "inherited": protected = False
    if fault == "null": entries = []
    if fault == "users": entries[1] = (0, 0, "S-1-5-32-545", 0x1F01FF)
    if fault == "unsupported": entries[1] = (5, 0, "S-1-5-32-544", 0x1F01FF)
    if fault == "generic": entries[1] = (0, 0, "S-1-5-32-544", 0x10000000)
    if fault == "inheritance": entries[1] = (0, 0x10, "S-1-5-32-544", 0x1F01FF)
    with pytest.raises(SecureScriptError):
        validate_security(owner, protected, entries)


def test_native_known_folder_ignores_environment_and_normal_token_cannot_create(monkeypatch):
    io = WindowsSecureScriptIO()
    expected = io.root()
    monkeypatch.setenv("ProgramData", "X:/untrusted")
    monkeypatch.setenv("TEMP", "X:/untrusted")
    assert io.root() == expected
    assert expected.name == "secure-runtime" and expected.parent.name == "TRAKFROM-BX"
    if not ctypes.windll.shell32.IsUserAnAdmin():
        with pytest.raises(SecureScriptError, match="elevation"):
            io.require_elevated()


def test_native_owner_acl_validation_rejects_user_owned_directory(tmp_path):
    io = WindowsSecureScriptIO()
    handle = io.open_directory(tmp_path)
    try:
        with pytest.raises(SecureScriptError):
            io.check(handle, tmp_path, True, True)
    finally:
        io.close(handle)

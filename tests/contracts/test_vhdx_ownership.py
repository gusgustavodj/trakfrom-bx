from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rekordbox_usb.source_write_guard import SourceWriteBoundary
from rekordbox_usb.vhdx_ownership import (
    VhdxFileIdentity,
    VhdxOwnershipDenial,
    VhdxOwnershipError,
    VhdxOwnershipLedger,
    VhdxOwnershipState,
)


def ledger_and_path(tmp_path: Path) -> tuple[VhdxOwnershipLedger, Path, Path]:
    source = tmp_path / "usb"
    source.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    return VhdxOwnershipLedger(managed, boundary=SourceWriteBoundary(source)), managed / "catalog.vhdx", source


def create_owned(
    ledger: VhdxOwnershipLedger, path: Path, *, source_id: str = "source-1"
):
    pending = ledger.begin_create(path, source_id)
    path.write_bytes(b"owned")
    return ledger.complete_create(pending, VhdxFileIdentity.observe(path))


def test_new_create_becomes_owned_with_physical_identity(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    record = create_owned(ledger, path)
    assert record.state is VhdxOwnershipState.OWNED_VALID
    assert record.identity is not None
    assert ledger.status(path, VhdxFileIdentity.observe(path)) is VhdxOwnershipState.OWNED_VALID


def test_preexisting_unknown_target_is_denied(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    path.write_bytes(b"foreign")
    with pytest.raises(VhdxOwnershipError) as raised:
        ledger.begin_create(path, "source-1", VhdxFileIdentity.observe(path))
    assert raised.value.code is VhdxOwnershipDenial.PREEXISTING_UNKNOWN


def test_owned_existing_with_same_identity_is_authorized(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    identity = VhdxFileIdentity.observe(path)
    capability = ledger.authorize(path, identity, "detach")
    capability.consume(identity)
    capability.assert_consumed(action="detach", path=path, current_identity=identity)


def test_same_path_with_replaced_file_is_denied(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    path.unlink()
    path.write_bytes(b"replacement")
    with pytest.raises(VhdxOwnershipError) as raised:
        ledger.authorize(path, VhdxFileIdentity.observe(path), "detach")
    assert raised.value.code is VhdxOwnershipDenial.IDENTITY_CHANGED


def test_absent_ledger_reports_unknown_without_creating_state(tmp_path: Path) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    ledger = VhdxOwnershipLedger(managed)
    assert ledger.status(managed / "missing.vhdx", None) is VhdxOwnershipState.UNKNOWN
    assert not ledger.path.exists()


@pytest.mark.parametrize("payload", ["{", "{}", '{"schema_version":1,"records":[]}'])
def test_invalid_or_truncated_ledger_fails_closed(tmp_path: Path, payload: str) -> None:
    managed = tmp_path / "managed"
    managed.mkdir()
    (managed / ".traktorbox-vhdx-ownership.json").write_text(payload, encoding="utf-8")
    with pytest.raises(VhdxOwnershipError) as raised:
        VhdxOwnershipLedger(managed).status(managed / "catalog.vhdx", None)
    assert raised.value.code is VhdxOwnershipDenial.LEDGER_INVALID


def test_generation_mismatch_is_denied(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    with pytest.raises(VhdxOwnershipError) as raised:
        ledger.authorize(
            path,
            VhdxFileIdentity.observe(path),
            "detach",
            expected_generation="stale-generation",
        )
    assert raised.value.code is VhdxOwnershipDenial.GENERATION_MISMATCH


def test_missing_owned_file_is_reported_without_side_effect(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    path.unlink()
    assert ledger.status(path, None) is VhdxOwnershipState.MISSING
    with pytest.raises(VhdxOwnershipError) as raised:
        ledger.authorize(path, None, "remove_vhdx")
    assert raised.value.code is VhdxOwnershipDenial.IDENTITY_MISSING


def test_restart_reloads_and_proves_the_same_identity(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    restarted = VhdxOwnershipLedger(ledger.storage_root)
    identity = VhdxFileIdentity.observe(path)
    assert restarted.authorize(path, identity, "recover").generation


def test_restart_rejects_a_replaced_identity(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    path.unlink()
    path.write_bytes(b"other")
    with pytest.raises(VhdxOwnershipError) as raised:
        VhdxOwnershipLedger(ledger.storage_root).authorize(
            path, VhdxFileIdentity.observe(path), "recover"
        )
    assert raised.value.code is VhdxOwnershipDenial.IDENTITY_CHANGED


def test_duplicate_concurrent_create_grants_one_generation(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)

    def reserve() -> str:
        return ledger.begin_create(path, "source-1").generation

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = []
        for future in (executor.submit(reserve), executor.submit(reserve)):
            try:
                outcomes.append(("ok", future.result()))
            except VhdxOwnershipError as exc:
                outcomes.append(("denied", exc.code.value))
    assert [kind for kind, _ in outcomes].count("ok") == 1
    assert [kind for kind, _ in outcomes].count("denied") == 1


def test_capability_is_single_use(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    identity = VhdxFileIdentity.observe(path)
    capability = ledger.authorize(path, identity, "detach")
    capability.consume(identity)
    with pytest.raises(VhdxOwnershipError) as raised:
        capability.consume(identity)
    assert raised.value.code is VhdxOwnershipDenial.CAPABILITY_USED


def test_capability_for_another_action_is_denied(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    identity = VhdxFileIdentity.observe(path)
    capability = ledger.authorize(path, identity, "detach")
    capability.consume(identity)
    with pytest.raises(VhdxOwnershipError) as raised:
        capability.assert_consumed(action="remove_vhdx", path=path, current_identity=identity)
    assert raised.value.code is VhdxOwnershipDenial.OPERATION_MISMATCH


def test_captured_p0_04_binding_survives_source_removal(tmp_path: Path) -> None:
    ledger, path, source = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    source.rmdir()
    restarted = VhdxOwnershipLedger(ledger.storage_root)
    identity = VhdxFileIdentity.observe(path)
    capability = restarted.authorize(path, identity, "detach")
    capability.consume(identity)
    restarted.record_effect(capability, VhdxOwnershipState.MANAGED_DETACHED)
    assert restarted.status(path, identity) is VhdxOwnershipState.MANAGED_DETACHED


def test_ledger_changes_only_after_confirmed_effect(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    create_owned(ledger, path)
    identity = VhdxFileIdentity.observe(path)
    capability = ledger.authorize(path, identity, "detach")
    capability.consume(identity)
    assert ledger.status(path, identity) is VhdxOwnershipState.OWNED_VALID
    ledger.record_effect(capability, VhdxOwnershipState.MANAGED_DETACHED)
    assert ledger.status(path, identity) is VhdxOwnershipState.MANAGED_DETACHED


def test_partial_artifact_requires_its_original_operation(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    pending = ledger.begin_create(path, "source-1")
    path.write_bytes(b"partial")
    partial = ledger.mark_partial(pending, VhdxFileIdentity.observe(path))
    identity = VhdxFileIdentity.observe(path)
    assert ledger.authorize(
        path,
        identity,
        "remove_vhdx",
        expected_generation=partial.generation,
        operation_id=partial.operation_id,
        allow_partial=True,
    )


def test_partial_artifact_rejects_another_operation(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    pending = ledger.begin_create(path, "source-1")
    path.write_bytes(b"partial")
    partial = ledger.mark_partial(pending, VhdxFileIdentity.observe(path))
    with pytest.raises(VhdxOwnershipError) as raised:
        ledger.authorize(
            path,
            VhdxFileIdentity.observe(path),
            "remove_vhdx",
            expected_generation=partial.generation,
            operation_id="another-operation",
            allow_partial=True,
        )
    assert raised.value.code is VhdxOwnershipDenial.OPERATION_MISMATCH


def test_unknown_foreign_file_is_classified_without_adoption(tmp_path: Path) -> None:
    ledger, path, _ = ledger_and_path(tmp_path)
    path.write_bytes(b"foreign")
    identity = VhdxFileIdentity.observe(path)
    assert ledger.status(path, identity) is VhdxOwnershipState.FOREIGN
    with pytest.raises(VhdxOwnershipError):
        ledger.authorize(path, identity, "detach")

from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from rekordbox_usb.in_memory_virtual_projection import ProjectionOperationResult
from rekordbox_usb.virtual_nml_catalog import NmlCatalog, NmlCatalogEntry
from rekordbox_usb.virtual_projection import ProjectionInvalidatedError, SourceAlreadyPublishedError, VirtualPath
from rekordbox_usb.windows_vhdx_nml_adapter import (
    WindowsVhdxAttachError,
    WindowsVhdxCreateError,
    WindowsVhdxDetachError,
    WindowsVhdxIncompatibleError,
    WindowsVhdxInspectionError,
    WindowsVhdxMountError,
    WindowsVhdxNmlVolumeAdapter,
    WindowsVhdxNmlVolumeConfig,
    WindowsVhdxPathOutsideStorageError,
    WindowsVhdxVolumeIdentityMismatchError,
    WindowsVhdxVolumeInspection,
)
from rekordbox_usb.vhdx_ownership import (
    VhdxFileIdentity,
    VhdxOwnershipDenial,
    VhdxOwnershipError,
    VhdxOwnershipState,
)


@pytest.fixture(autouse=True)
def synthetic_source_for_adapter_tests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bind every fake publication to a real, distinct synthetic source."""
    source = tmp_path / "usb"
    source.mkdir()
    original = WindowsVhdxNmlVolumeAdapter.__init__

    def initialize(self, *args, **kwargs):
        kwargs.setdefault("source_root", source)
        original(self, *args, **kwargs)

    monkeypatch.setattr(WindowsVhdxNmlVolumeAdapter, "__init__", initialize)


class FakeBackend:
    def __init__(self, path: Path, mount: Path) -> None:
        self.path = path
        self.mount = mount
        self.state = WindowsVhdxVolumeInspection(path, False, False, False, None, None, None)
        self.calls: list[str] = []
        self.ownership_actions: list[str] = []
        self.failure: str | None = None

    def set_ownership_capability(self, capability) -> None:
        self.ownership_actions.append(capability.action)

    def create_vhdx(self, path: Path, logical_size_mb: int, timeout_seconds: float) -> None:
        self.calls.append("create")
        if self.failure == "create_before":
            raise OSError("create before file")
        path.touch(exist_ok=False)
        self.state = replace(
            self.state,
            exists=True,
            physical_identity=VhdxFileIdentity.observe(path),
        )
        if self.failure == "create":
            raise OSError("create")

    def attach(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("attach")
        if self.failure == "attach":
            raise OSError("attach")
        self.state = replace(self.state, attached=True)

    def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("initialize")
        self.state = replace(self.state, initialized=True, partition_present=True)

    def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
        self.calls.append("format")
        self.state = replace(self.state, filesystem=filesystem, volume_label=label, partition_present=True)

    def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path:
        self.calls.append("mount")
        if self.failure == "mount":
            raise OSError("mount")
        self.mount.mkdir(exist_ok=True)
        self.state = replace(self.state, mount_point=self.mount)
        return self.mount

    def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
        self.calls.append("inspect")
        if self.failure == "inspect":
            raise OSError("inspect")
        if self.state.exists and not path.exists():
            path.touch()
        self.state = replace(
            self.state,
            physical_identity=(VhdxFileIdentity.observe(path) if self.state.exists else None),
        )
        return self.state

    def detach(self, path: Path, timeout_seconds: float) -> None:
        self.calls.append("detach")
        self.state = replace(self.state, attached=False)

    def remove_vhdx(self, path: Path) -> None:
        self.calls.append("remove")
        path.unlink(missing_ok=True)
        self.state = replace(self.state, exists=False, physical_identity=None)


def catalog(snapshot: str = "r1", source: str = "source-1") -> NmlCatalog:
    content = "<NML VERSION=\"19\"/>\n"
    entry = NmlCatalogEntry(
        source, "playlist-1", "set.nml", "Set", VirtualPath(("playlists", "set.nml")), content,
        sha256(content.encode()).hexdigest(), 0, snapshot,
    )
    return NmlCatalog(source, snapshot, (entry,))


def configured(tmp_path: Path, *, remove: bool = False) -> tuple[WindowsVhdxNmlVolumeAdapter, FakeBackend]:
    root = tmp_path / "managed"
    root.mkdir(exist_ok=True)
    path = root / "catalog.vhdx"
    backend = FakeBackend(path, tmp_path / "mount")
    config = WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB", remove_vhdx_on_unpublish=remove)
    return WindowsVhdxNmlVolumeAdapter(config, backend), backend


def detach_authorization(adapter: WindowsVhdxNmlVolumeAdapter, backend: FakeBackend):
    ledger = adapter._ownership_ledger
    generation = "missing"
    if ledger is not None and backend.state.exists:
        capability = ledger.authorize(
            adapter._config.vhdx_path,
            backend.inspect(adapter._config.vhdx_path).physical_identity,
            "detach",
        )
        generation = capability.generation
    return SimpleNamespace(
        backend=backend,
        path=adapter._config.vhdx_path,
        generation=generation,
    )


def test_initial_publish_creates_mounts_and_materializes_after_mount(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    result = adapter.publish(catalog())
    assert result.operation is ProjectionOperationResult.PUBLISHED
    assert result.state.mounted and result.state.mount_point == backend.mount
    assert backend.calls.index("mount") < len(backend.calls)
    assert (backend.mount / "playlists" / "set.nml").is_file()
    assert {"create", "attach", "initialize", "format", "mount"} <= set(backend.calls)
    assert {"attach", "initialize", "format", "assign_mount"} <= set(
        backend.ownership_actions
    )
    assert "initialize_if_needed" not in backend.ownership_actions
    assert "format_if_needed" not in backend.ownership_actions


def test_duplicate_publish_is_rejected(tmp_path: Path) -> None:
    adapter, _ = configured(tmp_path)
    adapter.publish(catalog())
    with pytest.raises(SourceAlreadyPublishedError):
        adapter.publish(catalog())


def test_unpublish_existing_detaches_only_the_configured_attached_vhdx(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    backend.calls.clear()

    result = adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, backend))

    assert result.changed is True
    assert result.mount_before == backend.mount
    assert result.mount_after is None
    assert backend.calls.count("detach") == 1
    assert backend.state.exists is True
    assert backend.state.attached is False


def test_unpublish_existing_rejects_a_missing_or_wrong_lifecycle_authorization(
    tmp_path: Path,
) -> None:
    adapter, backend = configured(tmp_path)
    backend.state = replace(backend.state, exists=True, attached=True, mount_point=backend.mount)

    with pytest.raises(WindowsVhdxVolumeIdentityMismatchError):
        adapter.unpublish_existing(detach_authorization=object())

    assert "detach" not in backend.calls


def test_unpublish_existing_is_gated_before_destructive_backend_call(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    backend.state = replace(backend.state, exists=True, attached=True, mount_point=backend.mount)
    with pytest.raises(WindowsVhdxDetachError, match="unable to detach") as raised:
        adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, backend))
    assert isinstance(raised.value.__cause__, VhdxOwnershipError)
    assert raised.value.__cause__.code is VhdxOwnershipDenial.OWNERSHIP_REQUIRED
    assert "detach" not in backend.calls


def test_unpublish_existing_is_idempotent_for_absent_or_detached_vhdx(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)

    absent = adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, backend))
    assert absent.changed is False
    assert absent.warnings == ("managed_vhdx_not_found",)
    assert "detach" not in backend.calls

    backend.state = replace(backend.state, exists=True, attached=False)
    detached = adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, backend))
    assert detached.changed is False
    assert detached.warnings == ()
    assert "detach" not in backend.calls


def test_unpublish_existing_blocks_a_mismatched_image_path(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    backend.state = replace(
        backend.state,
        vhdx_path=tmp_path / "other.vhdx",
        exists=True,
        attached=True,
    )

    with pytest.raises(WindowsVhdxVolumeIdentityMismatchError):
        adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, backend))

    assert "detach" not in backend.calls


def test_unpublish_existing_treats_detach_race_as_idempotent(tmp_path: Path) -> None:
    class RacingBackend(FakeBackend):
        def detach(self, path: Path, timeout_seconds: float) -> None:
            self.calls.append("detach")
            self.state = replace(self.state, attached=False)
            raise OSError("already detached")

    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    racing = RacingBackend(backend.path, backend.mount)
    racing.state = replace(backend.state, attached=True, mount_point=racing.mount)
    adapter = WindowsVhdxNmlVolumeAdapter(
        adapter._config, racing, ownership_ledger=adapter._ownership_ledger
    )
    racing.calls.clear()

    result = adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, racing))

    assert result.changed is False
    assert racing.calls.count("detach") == 1


def test_unpublish_existing_preserves_real_detach_failure_diagnostics(tmp_path: Path) -> None:
    class FailingBackend(FakeBackend):
        def detach(self, path: Path, timeout_seconds: float) -> None:
            self.calls.append("detach")
            raise OSError("held by Traktor")

    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    failing = FailingBackend(backend.path, backend.mount)
    failing.state = replace(backend.state, attached=True, mount_point=failing.mount)
    adapter = WindowsVhdxNmlVolumeAdapter(
        adapter._config, failing, ownership_ledger=adapter._ownership_ledger
    )
    failing.calls.clear()

    with pytest.raises(WindowsVhdxDetachError) as exc_info:
        adapter.unpublish_existing(detach_authorization=detach_authorization(adapter, failing))

    diagnostic = exc_info.value.diagnostic()
    assert diagnostic["lifecycle_stage"] == "detach"
    assert diagnostic["backend_operation"] == "detach"
    assert diagnostic["cause_type"] == "OSError"
    assert diagnostic["cause_message"] == "held by Traktor"


def test_compatible_existing_vhdx_is_reused_without_initialization_or_format(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    backend.state = replace(backend.state, attached=False, mount_point=None)
    backend.calls.clear()
    WindowsVhdxNmlVolumeAdapter(
        adapter._config, backend, ownership_ledger=adapter._ownership_ledger
    ).publish(catalog())
    assert "create" not in backend.calls
    assert backend.calls.count("attach") == 1
    assert "initialize" not in backend.calls
    assert "format" not in backend.calls


def test_attached_managed_vhdx_reuses_its_existing_mount_without_attach(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    backend.calls.clear()

    result = WindowsVhdxNmlVolumeAdapter(
        adapter._config, backend, ownership_ledger=adapter._ownership_ledger
    ).publish(catalog())

    assert result.operation is ProjectionOperationResult.PUBLISHED
    assert result.state.mount_point == backend.mount
    assert "attach" not in backend.calls
    assert "initialize" not in backend.calls
    assert "format" not in backend.calls


def test_attached_managed_vhdx_without_mount_is_reconciled_without_format(tmp_path: Path) -> None:
    class AttachedWithoutMountBackend(FakeBackend):
        def assign_mount(
            self,
            path: Path,
            preferred_drive_letter: str | None,
            timeout_seconds: float,
        ) -> Path:
            result = super().assign_mount(path, preferred_drive_letter, timeout_seconds)
            self.state = replace(
                self.state,
                filesystem="NTFS",
                volume_label="TRAKTORBOX_USB",
            )
            return result

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = AttachedWithoutMountBackend(path, tmp_path / "mount")
    adapter = WindowsVhdxNmlVolumeAdapter(
        WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB"),
        backend,
    )
    adapter.publish(catalog())
    backend.state = replace(backend.state, mount_point=None, attached=True)
    backend.calls.clear()

    result = WindowsVhdxNmlVolumeAdapter(
        adapter._config, backend, ownership_ledger=adapter._ownership_ledger
    ).publish(catalog())

    assert result.state.mount_point == backend.mount
    assert "attach" not in backend.calls
    assert "format" not in backend.calls


def test_attach_race_reinspects_the_same_managed_vhdx(tmp_path: Path) -> None:
    class AlreadyAttachedBackend(FakeBackend):
        def attach(self, path: Path, timeout_seconds: float) -> None:
            self.calls.append("attach")
            self.state = replace(self.state, attached=True)
            raise OSError("virtual disk is already attached")

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = AlreadyAttachedBackend(path, tmp_path / "mount")
    adapter = WindowsVhdxNmlVolumeAdapter(
        WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB"),
        backend,
    )

    result = adapter.publish(catalog())

    assert result.operation is ProjectionOperationResult.PUBLISHED
    assert backend.calls.count("attach") == 1


def test_attach_race_does_not_reuse_an_unrelated_attached_disk(tmp_path: Path) -> None:
    class DifferentManagedPathBackend(FakeBackend):
        def attach(self, path: Path, timeout_seconds: float) -> None:
            self.calls.append("attach")
            self.state = replace(self.state, attached=True)
            raise OSError("virtual disk is already attached")

        def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
            self.calls.append("inspect")
            if self.state.attached:
                return replace(self.state, vhdx_path=path.with_name("other.vhdx"))
            return self.state

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = DifferentManagedPathBackend(path, tmp_path / "mount")
    adapter = WindowsVhdxNmlVolumeAdapter(
        WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB"),
        backend,
    )

    with pytest.raises(WindowsVhdxAttachError) as error:
        adapter.publish(catalog())

    assert error.value.cause_type == "OSError"
    assert error.value.attached_before_failure is False


def test_second_publish_with_identical_catalog_is_unchanged_and_reuses_mount(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    first = adapter.publish(catalog())
    second = WindowsVhdxNmlVolumeAdapter(adapter._config, backend).publish(catalog())

    assert first.operation is ProjectionOperationResult.PUBLISHED
    assert second.operation is ProjectionOperationResult.PUBLISHED
    assert second.materialization is not None and not second.materialization.changed
    assert second.state.mount_point == first.state.mount_point
    assert backend.calls.count("attach") == 1


def test_incompatible_existing_vhdx_is_blocked_without_format(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    backend.state = replace(backend.state, filesystem="FAT32", volume_label="OTHER")
    backend.calls.clear()
    with pytest.raises(WindowsVhdxIncompatibleError):
        WindowsVhdxNmlVolumeAdapter(
            adapter._config, backend, ownership_ledger=adapter._ownership_ledger
        ).publish(catalog())
    assert "format" not in backend.calls


def test_attach_and_mount_failures_are_typed(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    backend.failure = "attach"
    with pytest.raises(WindowsVhdxAttachError):
        adapter.publish(catalog())
    adapter, backend = configured(tmp_path)
    backend.failure = "mount"
    with pytest.raises(WindowsVhdxMountError):
        adapter.publish(catalog())


def test_backend_failures_preserve_the_real_stage_cause_and_safe_rollback(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    backend.failure = "create"

    with pytest.raises(WindowsVhdxCreateError) as create_error:
        adapter.publish(catalog())

    assert create_error.value.lifecycle_stage == "create"
    assert create_error.value.backend_operation == "create_vhdx"
    assert create_error.value.underlying_error == "OSError"
    assert create_error.value.cause_type == "OSError"
    assert create_error.value.cause_message == "create"
    assert create_error.value.vhdx_path == backend.path
    assert create_error.value.created_before_failure is False
    assert create_error.value.attached_before_failure is False
    assert "remove" in backend.calls
    assert not backend.state.exists
    assert adapter._ownership_ledger is not None
    assert (
        adapter._ownership_ledger.status(backend.path, None)
        is VhdxOwnershipState.MISSING
    )

    adapter, backend = configured(tmp_path)
    backend.failure = "attach"
    with pytest.raises(WindowsVhdxAttachError) as attach_error:
        adapter.publish(catalog())
    assert attach_error.value.lifecycle_stage == "attach"
    assert attach_error.value.backend_operation == "attach"
    assert attach_error.value.cause_type == "OSError"
    assert "remove" in backend.calls

    adapter, backend = configured(tmp_path)
    backend.failure = "inspect"
    with pytest.raises(WindowsVhdxInspectionError) as inspect_error:
        adapter.publish(catalog())
    assert inspect_error.value.lifecycle_stage == "inspect"
    assert inspect_error.value.backend_operation == "inspect"
    assert inspect_error.value.cause_type == "OSError"


def test_create_failure_before_file_marks_missing_and_allows_retry(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    backend.failure = "create_before"

    with pytest.raises(WindowsVhdxCreateError):
        adapter.publish(catalog())

    assert not backend.path.exists()
    assert adapter._ownership_ledger is not None
    assert (
        adapter._ownership_ledger.status(backend.path, None)
        is VhdxOwnershipState.MISSING
    )

    retry_backend = FakeBackend(backend.path, backend.mount)
    retry = WindowsVhdxNmlVolumeAdapter(
        adapter._config,
        retry_backend,
        ownership_ledger=adapter._ownership_ledger,
    )
    assert retry.publish(catalog()).operation is ProjectionOperationResult.PUBLISHED


def test_refresh_noop_changed_and_invalidation_lifecycle(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    adapter.publish(catalog())
    assert adapter.refresh(catalog()).operation is ProjectionOperationResult.UNCHANGED
    assert adapter.refresh(catalog("r2")).operation is ProjectionOperationResult.REFRESHED
    authorization = detach_authorization(adapter, backend)
    assert adapter.invalidate(
        "source-1", detach_authorization=authorization
    ).operation is ProjectionOperationResult.INVALIDATED
    assert adapter.invalidate(
        "source-1", detach_authorization=authorization
    ).operation is ProjectionOperationResult.ALREADY_INVALIDATED
    with pytest.raises(ProjectionInvalidatedError):
        adapter.refresh(catalog("r3"))
    assert adapter.unpublish(
        "source-1", detach_authorization=authorization
    ).operation is ProjectionOperationResult.UNPUBLISHED
    assert "detach" in backend.calls


def test_unpublish_removal_is_limited_to_configured_root(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path, remove=True)
    adapter.publish(catalog())
    adapter.unpublish(
        "source-1", detach_authorization=detach_authorization(adapter, backend)
    )
    assert "remove" in backend.calls
    outside = tmp_path / "outside.vhdx"
    with pytest.raises(WindowsVhdxPathOutsideStorageError):
        WindowsVhdxNmlVolumeConfig(tmp_path / "managed", outside, 64, "TRAKTORBOX_USB")


def test_capabilities_are_explicit_and_no_physical_disk_identifier_exists(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    assert adapter.capabilities().read_only
    assert not adapter.capabilities().random_access
    assert adapter.capabilities().dynamic_refresh and adapter.capabilities().invalidation
    assert not adapter.capabilities().stable_mount_name and not adapter.capabilities().multiple_sources
    assert not hasattr(backend.state, "disk_number")


def test_publish_waits_for_partition_and_filesystem_before_materialization(tmp_path: Path) -> None:
    class DelayedBackend(FakeBackend):
        pending_partition = 0
        pending_filesystem = 0

        def initialize_if_needed(self, path: Path, timeout_seconds: float) -> None:
            super().initialize_if_needed(path, timeout_seconds)
            self.pending_partition = 1

        def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
            super().format_if_needed(path, filesystem, label, timeout_seconds)
            self.pending_filesystem = 1

        def inspect(self, path: Path) -> WindowsVhdxVolumeInspection:
            self.calls.append("inspect")
            if self.pending_partition:
                self.pending_partition -= 1
                return replace(self.state, partition_present=False, filesystem=None, volume_label=None)
            if self.pending_filesystem:
                self.pending_filesystem -= 1
                return replace(self.state, filesystem=None, volume_label=None)
            return self.state

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = DelayedBackend(path, tmp_path / "mount")
    adapter = WindowsVhdxNmlVolumeAdapter(WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB"), backend)
    result = adapter.publish(catalog())
    assert result.operation is ProjectionOperationResult.PUBLISHED
    assert backend.calls.index("format") < backend.calls.index("mount")
    assert (backend.mount / "playlists" / "set.nml").is_file()


def test_missing_filesystem_or_mount_times_out_before_materialization(tmp_path: Path) -> None:
    class NeverReadyBackend(FakeBackend):
        def format_if_needed(self, path: Path, filesystem: str, label: str, timeout_seconds: float) -> None:
            self.calls.append("format")
            self.state = replace(self.state, partition_present=True)

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = NeverReadyBackend(path, tmp_path / "mount")
    adapter = WindowsVhdxNmlVolumeAdapter(WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB", timeout_seconds=0.01), backend)
    with pytest.raises(Exception, match="filesystem and volume label"):
        adapter.publish(catalog())
    assert not (backend.mount / "playlists" / "set.nml").exists()
    assert "detach" in backend.calls


def test_missing_mount_point_times_out_before_materialization(tmp_path: Path) -> None:
    class NeverMountedBackend(FakeBackend):
        def assign_mount(self, path: Path, preferred_drive_letter: str | None, timeout_seconds: float) -> Path:
            self.calls.append("mount")
            return self.mount

    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = NeverMountedBackend(path, tmp_path / "mount")
    adapter = WindowsVhdxNmlVolumeAdapter(WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB", timeout_seconds=0.01), backend)
    with pytest.raises(Exception, match="mount point"):
        adapter.publish(catalog())
    assert not (backend.mount / "playlists" / "set.nml").exists()
    assert "detach" in backend.calls


def test_new_blank_vhdx_records_format_before_readiness_and_materialization(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    path = root / "catalog.vhdx"
    backend = FakeBackend(path, tmp_path / "mount")
    events: list[str] = []
    adapter = WindowsVhdxNmlVolumeAdapter(
        WindowsVhdxNmlVolumeConfig(root, path, 64, "TRAKTORBOX_USB"), backend, lifecycle_sink=events.append
    )
    adapter.publish(catalog())
    assert events == [
        "created", "attached", "initialized", "partition_ready", "format_requested", "format_completed",
        "filesystem_ready", "mount_requested", "mount_ready", "materialization_started",
    ]
    assert backend.calls.index("format") < backend.calls.index("mount")


def test_preexisting_blank_vhdx_is_blocked_without_format(tmp_path: Path) -> None:
    adapter, backend = configured(tmp_path)
    backend.state = replace(backend.state, exists=True, initialized=True, partition_present=True)
    with pytest.raises(WindowsVhdxIncompatibleError, match="ownership is not proved"):
        adapter.publish(catalog())
    assert "format" not in backend.calls

import json
from pathlib import Path

from rekordbox_usb.windows_drives import (
    DriveInfo,
    build_rekordbox_preflight_report,
    PreflightReason,
    PreflightStatus,
    RekordboxStructureKind,
    VolumeMetadata,
    detect_rekordbox_structures,
    filter_rekordbox_drives,
    has_rekordbox_library,
    list_windows_volumes,
    preflight_rekordbox_volumes,
)


class FakeWindowsVolumeApi:
    def __init__(self):
        self.metadata_calls: list[str] = []
        self.drive_type_calls: list[str] = []

    def logical_drive_mask(self) -> int:
        return (1 << 2) | (1 << 3) | (1 << 13)

    def drive_type(self, root: str) -> int:
        self.drive_type_calls.append(root)
        return {"C:\\": 3, "D:\\": 2, "N:\\": 4}[root]

    def volume_metadata(self, root: str) -> VolumeMetadata:
        self.metadata_calls.append(root)
        return {
            "C:\\": VolumeMetadata(label="System", serial="A1B2C3D4", filesystem="NTFS"),
            "D:\\": VolumeMetadata(label="DJ USB", serial="00112233", filesystem="FAT32"),
            "N:\\": VolumeMetadata(),
        }[root]


class FakeVolumePathProbe:
    def __init__(self, responses: dict[Path, bool | OSError]):
        self.responses = responses
        self.calls: list[Path] = []

    def is_file(self, path: Path) -> bool:
        self.calls.append(path)
        response = self.responses.get(path, False)
        if isinstance(response, OSError):
            raise response
        return response


def test_list_windows_volumes_returns_all_mounted_volumes_with_shallow_metadata():
    api = FakeWindowsVolumeApi()

    result = list_windows_volumes(api)

    assert [(item.root, item.label, item.serial, item.filesystem, item.drive_type) for item in result] == [
        (Path("C:/"), "System", "A1B2C3D4", "NTFS", 3),
        (Path("D:/"), "DJ USB", "00112233", "FAT32", 2),
        (Path("N:/"), None, None, None, 4),
    ]
    assert api.metadata_calls == ["C:\\", "D:\\", "N:\\"]
    assert api.drive_type_calls == ["C:\\", "D:\\", "N:\\"]


def test_has_rekordbox_device_library(tmp_path: Path):
    marker = tmp_path / "PIONEER" / "rekordbox" / "export.pdb"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"db")

    assert has_rekordbox_library(tmp_path)


def test_detect_rekordbox_structures_returns_evidence_for_each_volume():
    first_volume = DriveInfo(root=Path("D:/"), label="DJ USB", serial="00112233")
    second_volume = DriveInfo(root=Path("E:/"), label="Other USB", serial="AABBCCDD")
    device_library = first_volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    one_library = first_volume.root / "PIONEER" / "rekordboxAgent" / "exportLibrary.db"
    probe = FakeVolumePathProbe({device_library: True, one_library: True})

    result = detect_rekordbox_structures([first_volume, second_volume], probe)

    assert [item.volume for item in result] == [first_volume, second_volume]
    assert [(item.kind, item.path) for item in result[0].evidence] == [
        (RekordboxStructureKind.DEVICE_LIBRARY, device_library),
        (RekordboxStructureKind.ONE_LIBRARY, one_library),
    ]
    assert result[0].warnings == ()
    assert result[1].evidence == ()
    assert result[1].warnings == ()
    assert len(probe.calls) == 6


def test_detect_rekordbox_structures_continues_after_an_inaccessible_marker():
    volume = DriveInfo(root=Path("D:/"))
    other_volume = DriveInfo(root=Path("E:/"))
    inaccessible_marker = volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    recognized_marker = volume.root / "PIONEER" / "rekordbox" / "exportLibrary.db"
    other_recognized_marker = other_volume.root / "PIONEER" / "rekordboxAgent" / "exportLibrary.db"
    probe = FakeVolumePathProbe(
        {
            inaccessible_marker: PermissionError("access denied"),
            recognized_marker: True,
            other_recognized_marker: True,
        }
    )

    result = detect_rekordbox_structures([volume, other_volume], probe)

    assert [(item.kind, item.path) for item in result[0].evidence] == [
        (RekordboxStructureKind.ONE_LIBRARY, recognized_marker),
    ]
    assert result[0].warnings == (
        f"Could not inspect Rekordbox marker {inaccessible_marker}: access denied",
    )
    assert [(item.kind, item.path) for item in result[1].evidence] == [
        (RekordboxStructureKind.ONE_LIBRARY, other_recognized_marker),
    ]
    assert result[1].warnings == ()
    assert len(probe.calls) == 6


def test_detect_rekordbox_structures_checks_only_authorized_marker_paths():
    volume = DriveInfo(root=Path("D:/"))
    probe = FakeVolumePathProbe({})

    result = detect_rekordbox_structures([volume], probe)

    assert len(result) == 1
    assert result[0].evidence == ()
    assert result[0].warnings == ()
    assert probe.calls == [
        volume.root / "PIONEER" / "rekordbox" / "export.pdb",
        volume.root / "PIONEER" / "rekordbox" / "exportLibrary.db",
        volume.root / "PIONEER" / "rekordboxAgent" / "exportLibrary.db",
    ]


def test_preflight_marks_volume_ready_when_marker_evidence_has_no_warnings():
    volume = DriveInfo(root=Path("D:/"))
    marker = volume.root / "PIONEER" / "rekordbox" / "export.pdb"

    result = preflight_rekordbox_volumes([volume], FakeVolumePathProbe({marker: True}))

    assert result[0].status is PreflightStatus.READY
    assert result[0].warnings == ()
    assert result[0].reasons == ()
    assert result[0].has_rekordbox_evidence is True
    assert [item.path for item in result[0].evidence] == [marker]


def test_preflight_preserves_warnings_when_marker_evidence_is_partial():
    volume = DriveInfo(root=Path("D:/"))
    inaccessible_marker = volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    marker = volume.root / "PIONEER" / "rekordbox" / "exportLibrary.db"

    result = preflight_rekordbox_volumes(
        [volume],
        FakeVolumePathProbe({inaccessible_marker: PermissionError("access denied"), marker: True}),
    )

    assert result[0].status is PreflightStatus.READY_WITH_WARNINGS
    assert result[0].warnings == (
        f"Could not inspect Rekordbox marker {inaccessible_marker}: access denied",
    )
    assert result[0].reasons == (PreflightReason.PARTIAL_DETECTION,)
    assert result[0].has_rekordbox_evidence is True


def test_preflight_marks_volume_without_known_markers_as_unsupported():
    volume = DriveInfo(root=Path("D:/"))

    result = preflight_rekordbox_volumes([volume], FakeVolumePathProbe({}))

    assert result[0].status is PreflightStatus.UNSUPPORTED
    assert result[0].warnings == ()
    assert result[0].reasons == (PreflightReason.NO_LIBRARY_FOUND,)
    assert result[0].evidence == ()
    assert result[0].has_rekordbox_evidence is False


def test_preflight_marks_unreadable_volume_as_blocked():
    volume = DriveInfo(root=Path("D:/"))
    marker = volume.root / "PIONEER" / "rekordbox" / "export.pdb"

    result = preflight_rekordbox_volumes(
        [volume],
        FakeVolumePathProbe({marker: PermissionError("access denied")}),
    )

    assert result[0].status is PreflightStatus.BLOCKED
    assert result[0].reasons == (PreflightReason.ACCESS_ERROR,)
    assert result[0].has_rekordbox_evidence is False


def test_preflight_classifies_multiple_volumes_independently():
    ready_volume = DriveInfo(root=Path("D:/"))
    warning_volume = DriveInfo(root=Path("E:/"))
    unsupported_volume = DriveInfo(root=Path("F:/"))
    blocked_volume = DriveInfo(root=Path("G:/"))
    ready_marker = ready_volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    warning_marker = warning_volume.root / "PIONEER" / "rekordbox" / "exportLibrary.db"
    warning_error = warning_volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    blocked_error = blocked_volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    probe = FakeVolumePathProbe(
        {
            ready_marker: True,
            warning_marker: True,
            warning_error: PermissionError("access denied"),
            blocked_error: PermissionError("access denied"),
        }
    )

    result = preflight_rekordbox_volumes(
        [ready_volume, warning_volume, unsupported_volume, blocked_volume], probe
    )

    assert [item.volume for item in result] == [
        ready_volume,
        warning_volume,
        unsupported_volume,
        blocked_volume,
    ]
    assert [item.status for item in result] == [
        PreflightStatus.READY,
        PreflightStatus.READY_WITH_WARNINGS,
        PreflightStatus.UNSUPPORTED,
        PreflightStatus.BLOCKED,
    ]


def test_preflight_report_serializes_classified_results_to_json_safe_primitives():
    volume = DriveInfo(
        root=Path("D:/"),
        label="DJ USB",
        serial="00112233",
        filesystem="FAT32",
        drive_type=2,
    )
    inaccessible_marker = volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    marker = volume.root / "PIONEER" / "rekordbox" / "exportLibrary.db"
    results = preflight_rekordbox_volumes(
        [volume],
        FakeVolumePathProbe({inaccessible_marker: PermissionError("access denied"), marker: True}),
    )

    report = build_rekordbox_preflight_report(results)

    assert report.schema_version == 1
    assert report.to_dict() == {
        "schema_version": 1,
        "volumes": [
            {
                "volume": {
                    "root": "D:\\",
                    "label": "DJ USB",
                    "serial": "00112233",
                    "filesystem": "FAT32",
                    "drive_type": 2,
                },
                "status": "ready_with_warnings",
                "warnings": [
                    f"Could not inspect Rekordbox marker {inaccessible_marker}: access denied",
                ],
                "reasons": ["partial_detection"],
                "evidence": [
                    {"kind": "one_library", "path": "D:\\PIONEER\\rekordbox\\exportLibrary.db"},
                ],
                "has_rekordbox_evidence": True,
            }
        ],
    }
    json.dumps(report.to_dict())


def test_preflight_report_orders_volumes_and_evidence_deterministically():
    first_volume = DriveInfo(root=Path("D:/"))
    second_volume = DriveInfo(root=Path("E:/"))
    first_device_marker = first_volume.root / "PIONEER" / "rekordbox" / "export.pdb"
    first_one_library_marker = first_volume.root / "PIONEER" / "rekordbox" / "exportLibrary.db"
    second_marker = second_volume.root / "PIONEER" / "rekordboxAgent" / "exportLibrary.db"
    probe = FakeVolumePathProbe(
        {
            first_device_marker: True,
            first_one_library_marker: True,
            second_marker: True,
        }
    )

    first_report = build_rekordbox_preflight_report(
        preflight_rekordbox_volumes([second_volume, first_volume], probe)
    )
    second_report = build_rekordbox_preflight_report(
        preflight_rekordbox_volumes([first_volume, second_volume], probe)
    )

    assert first_report.to_dict() == second_report.to_dict()
    assert [item.root for item in first_report.volumes] == ["D:\\", "E:\\"]
    assert [item.path for item in first_report.volumes[0].evidence] == [
        "D:\\PIONEER\\rekordbox\\export.pdb",
        "D:\\PIONEER\\rekordbox\\exportLibrary.db",
    ]


def test_has_rekordbox_one_library_agent_path(tmp_path: Path):
    marker = tmp_path / "PIONEER" / "rekordboxAgent" / "exportLibrary.db"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"db")

    assert has_rekordbox_library(tmp_path)


def test_filter_accepts_usb_reported_as_fixed(tmp_path: Path):
    fixed_usb = tmp_path / "fixed-usb"
    normal_disk = tmp_path / "normal-disk"
    marker = fixed_usb / "PIONEER" / "rekordbox" / "exportLibrary.db"
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b"db")
    normal_disk.mkdir()

    result = filter_rekordbox_drives(
        [
            DriveInfo(root=fixed_usb, drive_type=3),
            DriveInfo(root=normal_disk, drive_type=3),
        ]
    )

    assert [item.root for item in result] == [fixed_usb]

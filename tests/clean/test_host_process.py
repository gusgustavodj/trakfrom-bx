from __future__ import annotations

from rekordbox_usb.host_process import (
    HostProcessState,
    TraktorHost,
    WindowsProcessEntry,
    WindowsTraktorProcessProbe,
)


TRAKTOR_PRO_2_PATH = r"C:\Program Files\Native Instruments\Traktor Pro 2\Traktor.exe"
TRAKTOR_PRO_3_PATH = r"C:\Program Files\Native Instruments\Traktor Pro 3\Traktor.exe"


def _probe(entries, paths) -> WindowsTraktorProcessProbe:
    return WindowsTraktorProcessProbe(
        lambda: entries,
        lambda pid: paths[pid],
    )


def test_exact_traktor_pro_4_executable_is_recognized_case_insensitively() -> None:
    probe = _probe(
        (WindowsProcessEntry(12, "TRAKTOR PRO 4.EXE"),),
        {},
    )

    assert probe.state() is HostProcessState.RUNNING
    assert probe.detected_host is TraktorHost.TRAKTOR_PRO_4


def test_traktor_pro_2_requires_its_exact_allowed_executable_path() -> None:
    probe = _probe(
        (WindowsProcessEntry(12, "Traktor.exe"),),
        {12: TRAKTOR_PRO_2_PATH},
    )

    assert probe.state() is HostProcessState.RUNNING
    assert probe.detected_host is TraktorHost.TRAKTOR_PRO_2


def test_traktor_pro_3_requires_its_exact_allowed_executable_path() -> None:
    probe = _probe(
        (WindowsProcessEntry(12, "Traktor.exe"),),
        {12: TRAKTOR_PRO_3_PATH},
    )

    assert probe.state() is HostProcessState.RUNNING
    assert probe.detected_host is TraktorHost.TRAKTOR_PRO_3


def test_legacy_host_name_and_path_comparisons_are_case_insensitive() -> None:
    probe = _probe(
        (WindowsProcessEntry(12, "tRaKtOr.ExE"),),
        {12: r"c:/PROGRAM FILES/native instruments/TRAKTOR PRO 3/traktor.EXE"},
    )

    assert probe.state() is HostProcessState.RUNNING
    assert probe.detected_host is TraktorHost.TRAKTOR_PRO_3


def test_false_traktor_executable_is_ignored_when_its_path_is_readable() -> None:
    probe = _probe(
        (WindowsProcessEntry(12, "Traktor.exe"),),
        {12: r"C:\Tools\Traktor.exe"},
    )

    assert probe.state() is HostProcessState.NOT_RUNNING
    assert probe.detected_host is None


def test_similar_process_names_do_not_block_detach() -> None:
    probe = _probe(
        (
            WindowsProcessEntry(12, "Traktor Helper.exe"),
            WindowsProcessEntry(13, "Traktor Pro 4.exe.bak"),
        ),
        {},
    )

    assert probe.state() is HostProcessState.NOT_RUNNING


def test_similar_legacy_path_does_not_block_detach() -> None:
    probe = _probe(
        (WindowsProcessEntry(12, "Traktor.exe"),),
        {12: r"C:\Program Files\Native Instruments\Traktor Pro 3 Backup\Traktor.exe"},
    )

    assert probe.state() is HostProcessState.NOT_RUNNING


def test_legacy_path_lookup_failure_is_unknown_with_structured_diagnostic() -> None:
    probe = WindowsTraktorProcessProbe(
        lambda: (WindowsProcessEntry(42, "Traktor.exe"),),
        lambda _pid: (_ for _ in ()).throw(PermissionError("access denied")),
    )

    assert probe.state() is HostProcessState.UNKNOWN
    assert probe.diagnostic() == {
        "stage": "QUERY_FULL_PROCESS_IMAGE_NAME",
        "error": "PermissionError",
        "message": "access denied",
        "pid": 42,
        "process_name": "Traktor.exe",
    }


def test_enumeration_failure_is_unknown_with_structured_diagnostic() -> None:
    probe = WindowsTraktorProcessProbe(
        lambda: (_ for _ in ()).throw(OSError("process inventory unavailable"))
    )

    assert probe.state() is HostProcessState.UNKNOWN
    assert probe.diagnostic()["stage"] == "PROCESS_ENUMERATION"
    assert probe.diagnostic()["error"] == "OSError"


def test_multiple_legacy_hosts_and_traktor_pro_4_are_running() -> None:
    probe = _probe(
        (
            WindowsProcessEntry(12, "Traktor.exe"),
            WindowsProcessEntry(13, "Traktor.exe"),
            WindowsProcessEntry(14, "Traktor Pro 4.exe"),
        ),
        {12: TRAKTOR_PRO_2_PATH, 13: TRAKTOR_PRO_3_PATH},
    )

    assert probe.state() is HostProcessState.RUNNING


def test_false_legacy_process_does_not_mask_a_path_query_failure() -> None:
    probe = WindowsTraktorProcessProbe(
        lambda: (
            WindowsProcessEntry(12, "Traktor.exe"),
            WindowsProcessEntry(13, "Traktor.exe"),
        ),
        lambda pid: (
            r"C:\Tools\Traktor.exe"
            if pid == 12
            else (_ for _ in ()).throw(OSError("query failed"))
        ),
    )

    assert probe.state() is HostProcessState.UNKNOWN


def test_no_host_is_not_running() -> None:
    probe = _probe((WindowsProcessEntry(12, "explorer.exe"),), {})

    assert probe.state() is HostProcessState.NOT_RUNNING

"""Exercise installer bodies with isolated Windows effects and removal plans."""
import json
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native PowerShell/COM contract")
ROOT = Path(__file__).resolve().parents[2]


def run_harness(tmp_path, choice="s", scenario="normal"):
    result = subprocess.run([
        "powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
        "-File", str(ROOT / "tests/fixtures/desktop_install_harness.ps1"),
        "-Source", str(ROOT), "-Work", str(tmp_path), "-Choice", choice, "-Scenario", scenario,
    ], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads(result.stdout[result.stdout.index("{"):])


@pytest.mark.parametrize("choice,expected", [("s", "yes"), ("n", "no"), ("", "no")])
def test_installer_prompt_start_menu_upgrade_and_bounded_uninstall(tmp_path, choice, expected):
    result = run_harness(tmp_path, choice)
    assert len(result["prompts"]) == 1 and "Windows" in result["prompts"][0]
    choices = [call["arguments"][-1] for call in result["calls"]
               if "configure-startup" in call["arguments"]]
    assert choices == [expected, "no", "no"]
    registration = result["registration"]
    assert registration["DisplayName"] == "TRAKFROM-BX"
    assert registration["InstallLocation"] == result["install_root"]
    assert "uninstall-windows.ps1" in registration["UninstallString"]
    assert registration["NoModify"] == registration["NoRepair"] == 1
    service, uninstaller = result["links"]
    assert service["target"].endswith("venv\\Scripts\\pythonw.exe")
    assert "public_launcher service" in service["arguments"]
    assert '"' in service["arguments"]  # Synthetic Local User contains spaces.
    assert "uninstall-windows.ps1" in uninstaller["arguments"]
    recursive = [entry["path"] for entry in result["removals"] if entry["recursive"]]
    assert recursive == [str(Path(result["install_root"]) / "venv")]
    assert len(result["removals"]) >= 7
    assert all(not entry["path"].startswith(result["data_root"]) for entry in result["removals"])
    assert result["preserved"] == "preserve"


@pytest.mark.parametrize("scenario", ["running", "foreign-link"])
def test_installer_rejects_active_runtime_and_foreign_links_before_python(tmp_path, scenario):
    result = run_harness(tmp_path, scenario=scenario)
    assert result["blocked"]
    assert not result["calls"] and not result["removals"]
    assert ("Close Traktor" if scenario == "running" else "collision") in result["failure"]


@pytest.mark.parametrize("suffix", ["", "launcher"])
def test_powershell_maintenance_observes_python_worker_and_tray_mutexes(tmp_path, suffix):
    from rekordbox_usb.public_watch import exclusive_runtime
    root = tmp_path / "Local User" / "RekordboxUSB"
    with exclusive_runtime(root / suffix):
        result = run_harness(tmp_path, scenario="external-running")
    assert result["blocked"] and "Close Traktor" in result["failure"]
    assert not result["calls"] and not result["removals"]

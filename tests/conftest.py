from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest


_MODE_ENVIRONMENT_VARIABLE = "TRAKTORBOX_TEST_EXECUTION_MODE"
_EXTERNAL_CWD_ENVIRONMENT_VARIABLE = "TRAKTORBOX_TEST_EXTERNAL_CWD"


def _remove_environment_variable(environment: dict[str, str], name: str) -> None:
    for key in tuple(environment):
        if key.casefold() == name.casefold():
            del environment[key]


@dataclass(frozen=True)
class PublicExecution:
    repository_root: Path

    def _mode(self) -> str:
        mode = os.environ.get(_MODE_ENVIRONMENT_VARIABLE, "source").casefold()
        if mode not in {"source", "installed"}:
            raise RuntimeError(
                f"{_MODE_ENVIRONMENT_VARIABLE} must be 'source' or 'installed', got {mode!r}"
            )
        return mode

    @property
    def is_installed(self) -> bool:
        return self._mode() == "installed"

    def _external_cwd(self) -> Path:
        value = os.environ.get(_EXTERNAL_CWD_ENVIRONMENT_VARIABLE)
        if not value:
            raise RuntimeError(
                f"{_EXTERNAL_CWD_ENVIRONMENT_VARIABLE} is required in installed mode"
            )
        external_cwd = Path(value)
        if not external_cwd.is_absolute() or not external_cwd.is_dir():
            raise RuntimeError(
                f"{_EXTERNAL_CWD_ENVIRONMENT_VARIABLE} must name an existing absolute directory"
            )
        resolved_cwd = external_cwd.resolve()
        resolved_repository = self.repository_root.resolve()
        if resolved_cwd == resolved_repository or resolved_repository in resolved_cwd.parents:
            raise RuntimeError(
                f"{_EXTERNAL_CWD_ENVIRONMENT_VARIABLE} must be outside the repository"
            )
        return resolved_cwd

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.pop("PYTHONHOME", None)
        if self._mode() == "source":
            environment["PYTHONPATH"] = str(self.repository_root / "src")
        else:
            _remove_environment_variable(environment, "PYTHONPATH")
            environment["PYTHONNOUSERSITE"] = "1"
            environment["PYTHONSAFEPATH"] = "1"
        return environment

    def cwd(self) -> Path:
        if self._mode() == "source":
            return self.repository_root
        return self._external_cwd()

    def script(self, script: str) -> str:
        if self._mode() == "source":
            return script
        source_root = os.path.normcase(os.path.realpath(self.repository_root / "src"))
        return (
            f"{script}\n"
            "import importlib\n"
            "import os\n"
            "import sys\n"
            f"_source_roots = ({source_root!r},)\n"
            "_normalise = lambda value: os.path.normcase(os.path.realpath(value))\n"
            "if any(any(_normalise(path) == root or _normalise(path).startswith(root + os.sep) "
            "for root in _source_roots) for path in sys.path if path):\n"
            "    raise AssertionError('installed subprocess sys.path contains a checkout source root')\n"
            "_module = importlib.import_module('rekordbox_usb')\n"
            "_module_path = _normalise(_module.__file__)\n"
            "if not any(part.casefold() in {'site-packages', 'dist-packages'} for part in _module_path.split(os.sep)):\n"
            "    raise AssertionError('rekordbox_usb was not imported from site-packages')\n"
            "if any(_module_path == root or _module_path.startswith(root + os.sep) for root in _source_roots):\n"
            "    raise AssertionError('rekordbox_usb was imported from the checkout')\n"
        )


@pytest.fixture
def execution() -> PublicExecution:
    return PublicExecution(repository_root=Path(__file__).resolve().parents[1])


def pytest_collection_modifyitems(items):
    # Classification only: no new skip and no weakening of native safety tests.
    from scripts.test_scope import CORE_FILES
    root = Path(__file__).resolve().parents[1]
    for item in items:
        relative = Path(item.path).relative_to(root).as_posix()
        marker = "core_portable" if relative in CORE_FILES else "windows_only"
        item.add_marker(getattr(pytest.mark, marker))

"""Positive source inventory and static private/secret checks, using only stdlib."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import tomllib


def check(root):
    allowed = set((root / "PUBLIC_FILES.txt").read_text(encoding="utf-8").splitlines())
    assert allowed and "PUBLIC_FILES.txt" in allowed
    ignored_dirs = {".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache"}
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in ignored_dirs or part.endswith(".egg-info") for part in relative.parts):
            continue
        assert not path.is_symlink(), relative
        if path.is_file():
            actual.add(relative.as_posix())
    assert actual == allowed, {"extra": sorted(actual - allowed), "missing": sorted(allowed - actual)}
    blocked_imports = {"rekordbox_usb_internal", "pyrekordbox", "sqlcipher3", "pysqlcipher3"}
    secrets = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-proj-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}")
    personal = re.compile(r"C:[\\/]Users[\\/]|/Users/", re.IGNORECASE)
    for name in sorted(allowed):
        if name == "TRAKFROM-BX-Setup.exe":
            payload = (root / name).read_bytes()
            assert len(payload) == 11441231, "Installer size mismatch"
            assert hashlib.sha256(payload).hexdigest() == (
                "07604299ec22e11b1d620f6b87c56911f16275885e9aa204506ad9a14e46350d"
            ), "Installer SHA-256 mismatch"
            continue
        data = (root / name).read_text(encoding="utf-8")
        # The scanner's rule strings are definitions, not credentials or user data.
        if name != "scripts/check_public_tree.py":
            assert not secrets.search(data), "Secret pattern: " + name
            assert not personal.search(data), "Personal path/name: " + name
        assert not any(name.lower().endswith(ext) for ext in (".db", ".pdb", ".sqlite", ".vhdx", ".mp3", ".wav", ".nml")), name
        if name.endswith(".py"):
            tree = ast.parse(data)
            for node in ast.walk(tree):
                imports = [n.name for n in node.names] if isinstance(node, ast.Import) else ([node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                assert not any(module.split(".")[0] in blocked_imports for module in imports), name
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    assert project["name"] == "trakfrom-bx" and project["version"] == "0.1.0.dev0"
    assert project["license"] == "MIT" and project["license-files"] == ["LICENSE"]
    assert project["dependencies"] == []
    assert "Operating System :: MacOS" not in project["classifiers"]
    assert (root / "LICENSE").read_text().startswith("MIT License\n")
    return {"public_files": len(allowed), "private_dependencies": 0, "secret_blockers": 0}


if __name__ == "__main__":
    print(json.dumps(check(Path(__file__).resolve().parents[1]), indent=2))

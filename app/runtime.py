"""Optional, operator-supplied runtime profile support.

The public package deliberately contains no personal skills, hooks, or runtime
profiles. Operators may explicitly provide a generic profile directory when
they need one; otherwise benchmark actors receive an empty fresh home.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path, PurePosixPath

PROFILE_DIRECTORY_ENV = "SHELLEY_BENCH_RUNTIME_PROFILE_DIR"


class RuntimeProfileUnavailable(ValueError):
    """Raised when a requested profile has not been configured by its operator."""


def _safe_path(name: str) -> bool:
    path = PurePosixPath(name)
    return (bool(name) and not path.is_absolute() and ".." not in path.parts
            and str(path) == name and path.parts[0] in {".config", "bin"})


def _profile_directory() -> Path:
    configured = os.environ.get(PROFILE_DIRECTORY_ENV)
    if not configured:
        raise RuntimeProfileUnavailable(
            f"Runtime profiles are unavailable. Set {PROFILE_DIRECTORY_ENV} to an "
            "operator-maintained generic profile directory to enable 'configured'.")
    directory = Path(configured).expanduser().resolve()
    if not directory.is_dir():
        raise RuntimeProfileUnavailable(f"{PROFILE_DIRECTORY_ENV} is not a directory")
    return directory


def freeze(name):
    """Freeze an explicitly configured generic profile into serializable text."""
    if not name:
        return None
    if name != "configured":
        raise RuntimeProfileUnavailable(
            "Only the explicitly configured runtime profile is available in the public package.")
    directory = _profile_directory()
    manifest_path = directory / "profile.json"
    try:
        manifest = json.loads(manifest_path.read_text())
        names = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeProfileUnavailable(
            f"{PROFILE_DIRECTORY_ENV} must contain profile.json with a files list") from exc
    if not isinstance(names, list) or not names or any(not isinstance(name, str) or not _safe_path(name) for name in names):
        raise RuntimeProfileUnavailable("Runtime profile files must be a non-empty safe relative-path list")
    files = {}
    for name in names:
        source = (directory / name).resolve()
        if directory not in source.parents or not source.is_file() or source.is_symlink():
            raise RuntimeProfileUnavailable(f"Runtime profile file is unavailable or unsafe: {name}")
        files[name] = source.read_text()
    return {"name": "configured", "files": files}


def install(home, profile):
    """Install only a validated frozen profile into an actor's fresh home."""
    if not profile:
        return
    files = profile.get("files", {})
    if (profile.get("name") != "configured" or not isinstance(files, dict) or not files
            or any(not isinstance(name, str) or not _safe_path(name) or not isinstance(content, str)
                   for name, content in files.items())):
        raise ValueError("Invalid frozen runtime profile")
    home = Path(home)
    for name, content in files.items():
        target = home / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        target.chmod(0o644 if name.endswith(".md") else 0o755)


def export(output, home):
    """Export operator-profile output only when it intentionally writes threads."""
    threads = Path(home) / "threads"
    if threads.is_dir():
        shutil.copytree(threads, Path(output) / "threads", dirs_exist_ok=True)

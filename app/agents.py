"""Select coding-agent adapters without changing task grading."""
from __future__ import annotations

import hashlib
from pathlib import Path
import shutil
import subprocess

from . import native


def pi_version(item, default_binary="pi"):
    requested = item.get("binary") or default_binary
    binary = shutil.which(requested)
    if not binary or not Path(binary).is_file():
        raise ValueError("Pi executable not found; configure pi_binary or provide binary")
    binary = str(Path(binary).resolve())
    result = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError("Pi version discovery failed")
    version = result.stdout.strip()
    options = item.get("options") or {}
    if not isinstance(options, dict) or set(options) - {"provider", "model_id", "extensions", "config_dir"}:
        raise ValueError("Unknown Pi adapter option")
    if any(key in options and not isinstance(options[key], str) for key in ("provider", "model_id", "config_dir")):
        raise ValueError("Pi provider, model_id, and config_dir must be strings")
    if "extensions" in options and (not isinstance(options["extensions"], list) or any(not isinstance(p, str) for p in options["extensions"])):
        raise ValueError("Pi extensions must be a list of paths")
    with Path(binary).open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    return {"agent": "pi", "binary": binary, "ref": f"Pi {version}", "sha": digest,
            "describe": {"subject": f"Pi {version}", "ref": f"Pi {version}"}, "options": options, "runtime_profile": None}


def prepare_pi(version, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    source, target = Path(version["binary"]), out / "pi"
    with source.open("rb") as handle:
        digest = hashlib.file_digest(handle, "sha256").hexdigest()
    if digest != version["sha"]:
        raise ValueError("Pi executable changed since benchmark creation")
    shutil.copy2(source, target)
    target.chmod(target.stat().st_mode | 0o111)
    return {"agent": "pi", "path": str(target.resolve()), "options": version.get("options") or {},
            "provenance": {"binary_sha256": digest, "agent": "pi", "version": version["ref"]}}


def run_agent(binary, work, output, prompt, model, *, variant, **kwargs):
    if variant.get("agent", "shelley") == "pi":
        from . import pi
        return pi.run_agent(binary, work, output, prompt, model, options=variant.get("options") or {}, **kwargs)
    return native.run_agent(binary, work, output, prompt, model, **kwargs)

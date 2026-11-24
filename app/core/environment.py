"""Inspect the host environment (spec section 18)."""
from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

TOOLS = ("git", "node", "npm", "uv", "pip3", "sqlite3", "docker", "curl")


def inspect_environment(project_root: Path) -> dict:
    du = shutil.disk_usage(project_root)
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                os_release[k] = v.strip('"')
    except OSError:
        pass
    mem_total = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal"):
                mem_total = round(int(line.split()[1]) / 1024 / 1024, 1)
    except OSError:
        pass
    writable = os.access(project_root, os.W_OK)
    return {
        "os": os_release.get("PRETTY_NAME", platform.platform()),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "memory_total_gb": mem_total,
        "disk_total_gb": round(du.total / 1e9, 1),
        "disk_free_gb": round(du.free / 1e9, 1),
        "tools": {t: shutil.which(t) for t in TOOLS},
        "project_root": str(project_root),
        "project_root_writable": writable,
        # /workspace persists across turns on this box, except dist/build/out/... folders
        "persistent_storage": writable and not any(
            part in {"dist", "build", "out", "coverage", "target"} for part in project_root.parts),
    }

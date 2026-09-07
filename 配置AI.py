"""Compatibility launcher for the personal 秋招雷达 service.

CCSwitch/Codex owns the provider credentials. This file deliberately does not
prompt for, persist, or print an API key.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main() -> int:
    script = ROOT / "启动秋招雷达.ps1"
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell and script.exists():
        completed = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=str(ROOT),
            check=False,
        )
        return completed.returncode
    # A small fallback keeps the launcher useful on systems without PowerShell.
    completed = subprocess.run([shutil.which("python") or "python", "-u", str(ROOT / "server.py")], cwd=str(ROOT), check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

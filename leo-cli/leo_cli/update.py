"""`leo update`: upgrade L.E.O CLI in place with uv, from wherever it was installed (git, a folder, or the npm launcher)."""
import os
import shutil
import subprocess
from pathlib import Path

PACKAGES = ("leo-cli", "leo-orchestrator")


def find_uv():
    """uv on PATH, or where its installer puts it."""
    found = shutil.which("uv")
    if found:
        return found
    name = "uv.exe" if os.name == "nt" else "uv"
    for folder in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        if (folder / name).is_file():
            return str(folder / name)
    return None


def command(uv):
    """Upgrade the leo-cli tool, rebuilding both L.E.O. packages so a git or folder install picks up new code."""
    return [uv, "tool", "upgrade", PACKAGES[0], *[arg for package in PACKAGES for arg in ("--reinstall-package", package)]]


def update(console, run=subprocess.call):
    uv = find_uv()
    if not uv:
        console.print("L.E.O CLI updates itself with uv, which is not installed here. Reinstall with "
                      "[bold]npm install -g leo-orchestrator[/] or see https://docs.astral.sh/uv/ to install uv.")
        return 1
    console.print("Updating L.E.O CLI…")
    code = run(command(uv))
    if code == 0:
        console.print("[leo.ok]L.E.O CLI is up to date.[/] Restart leo to use the new version.")
        return 0
    console.print(f"[leo.error]The update did not finish (uv exited with {code}).[/] If leo is open in another window, close it "
                  f"and run [bold]{' '.join(command('uv'))}[/].")
    return code

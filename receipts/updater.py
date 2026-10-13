"""Self-update from GitHub: compare version.json, download that commit, swap the code in, restart.

Only the app's own files are replaced. Settings live in %APPDATA%\\Receipts and installed packages
in .venv, so both survive. If requirements.txt changed, the launcher installs packages on restart.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import NamedTuple

from . import UPDATE_BRANCH, UPDATE_REPO

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent
PACKAGE = "receipts"
_SKIP = {".venv", ".git", "__pycache__", ".github"}


class Release(NamedTuple):
    version: str
    notes: str
    sha: str


def local_version() -> str:
    try:
        return json.loads((APP_DIR / "version.json").read_text("utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return "0"


def local_notes() -> str:
    try:
        return json.loads((APP_DIR / "version.json").read_text("utf-8")).get("notes", "")
    except (OSError, ValueError):
        return ""


def blocked_reason() -> str | None:
    """Why this copy can't update itself, or None if it can."""
    if (APP_DIR / ".git").exists():
        return "This copy is a git checkout; update it with git pull instead."
    if not os.access(APP_DIR, os.W_OK):
        return f"Can't write to {APP_DIR}. Move the Receipts folder somewhere you own, like Documents."
    return None


def check() -> Release | None:
    """The latest release on GitHub if it's newer than this install, else None."""
    sha = json.loads(_get(f"https://api.github.com/repos/{UPDATE_REPO}/commits/{UPDATE_BRANCH}"))["sha"]
    meta = json.loads(_get(f"https://raw.githubusercontent.com/{UPDATE_REPO}/{sha}/version.json"))
    if _version_tuple(meta["version"]) > _version_tuple(local_version()):
        return Release(meta["version"], meta.get("notes", ""), sha)
    return None


def install(release: Release) -> None:
    """Download `release` and copy it over this install. Call relaunch() afterwards."""
    if reason := blocked_reason():
        raise RuntimeError(reason)
    data = _get(f"https://api.github.com/repos/{UPDATE_REPO}/zipball/{release.sha}")
    with tempfile.TemporaryDirectory() as tmp:
        archive, extract = Path(tmp) / "update.zip", Path(tmp) / "x"
        archive.write_bytes(data)
        with zipfile.ZipFile(archive) as z:
            for member in z.namelist():  # refuse anything that would land outside the temp dir
                if not (extract / member).resolve().is_relative_to(extract.resolve()):
                    raise RuntimeError(f"Unsafe path in update: {member}")
            z.extractall(extract)
        roots = [p.parent.parent for p in extract.rglob("__main__.py") if p.parent.name == PACKAGE]
        if len(roots) != 1 or not (roots[0] / "version.json").exists():
            raise RuntimeError("The downloaded update doesn't look like Receipts.")
        _copy_over(roots[0], APP_DIR)
    log.info("Installed version %s (%s)", release.version, release.sha[:7])


def relaunch() -> None:
    """Start a fresh copy (after the current one exits) through the launcher."""
    if sys.platform == "win32":
        # The launcher reinstalls packages if requirements.txt changed, then starts the app.
        subprocess.Popen(["cmd", "/c", "call", "Start Receipts.bat", "--after-update"], cwd=APP_DIR,
                         creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        subprocess.Popen([sys.executable, "-m", PACKAGE, "--after-update"], cwd=APP_DIR, start_new_session=True)


def _copy_over(src: Path, dst: Path) -> None:
    # Drop modules that the new version no longer has, so stale code can't be imported.
    new_files = {p.name for p in (src / PACKAGE).iterdir() if p.is_file()}
    for old in (dst / PACKAGE).glob("*.py"):
        if old.name not in new_files:
            old.unlink()
    version_file = None
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if path.is_dir() or _SKIP.intersection(rel.parts):
            continue
        if rel == Path("version.json"):
            version_file = path  # written last: a half-finished copy must not claim the new version
            continue
        _copy_file(path, dst / rel)
    if version_file:
        _copy_file(version_file, dst / "version.json")


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".bat":  # cmd.exe misreads labels in LF-only batch files
        text = src.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        dst.write_bytes(text)
    else:
        shutil.copyfile(src, dst)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Receipts-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _version_tuple(v: str) -> tuple:
    return tuple(int(part) if part.isdigit() else 0 for part in str(v).split("."))

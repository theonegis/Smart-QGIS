"""Launch the distributor's Python, never mix its Qt/GDAL with the MCP venv."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def _bundled_grass_base() -> Path | None:
    """Return the newest valid standalone macOS GRASS application base."""
    candidates = [
        app / "Contents/Resources"
        for app in Path("/Applications").glob("GRASS-*.app")
        if (app / "Contents/Resources/etc/VERSIONNUMBER").is_file()
        and (app / "Contents/Resources/bin/grass").is_file()
    ]
    return max(candidates, key=lambda folder: folder.parent.parent.name, default=None)


def _first_existing(paths):
    return next((path for path in paths if path.is_file()), None)


def _first_directory(paths):
    return next((path for path in paths if path.is_dir()), None)


def _set_path(env, *folders):
    entries = [str(folder) for folder in folders if folder and Path(folder).is_dir()]
    entries.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(entry for entry in entries if entry)


def _windows_qgis_prefix(env: dict[str, str]) -> Path | None:
    configured = env.get("SMART_QGIS_PREFIX_PATH") or env.get("QGIS_PREFIX_PATH")
    if configured:
        return Path(configured)
    program_files = [env.get("ProgramW6432"), env.get("ProgramFiles")]
    candidates = []
    for value in program_files:
        if value:
            candidates.extend(Path(value).glob("QGIS*/apps/qgis"))
    return max((path for path in candidates if path.is_dir()), key=str, default=None)


def _configure_macos(env: dict[str, str]) -> str | None:
    app = Path(env.get("SMART_QGIS_APP", "/Applications/QGIS.app"))
    if not app.is_dir():
        return None
    contents = app / "Contents"
    resources = contents / "Resources/qgis"
    env.setdefault("QGIS_PREFIX_PATH", str(app))
    env.setdefault("PROJ_DATA", str(resources / "proj"))
    env.setdefault("GDAL_DATA", str(resources / "gdal"))
    _set_path(env, contents / "MacOS", Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
    if "GISBASE" not in env:
        grass_base = _bundled_grass_base()
        if grass_base:
            env["GISBASE"] = str(grass_base)
    env["SMART_QGIS_PLUGIN_PATH"] = str(resources / "python/plugins")
    return str(contents / "MacOS/python")


def _configure_windows(env: dict[str, str]) -> str | None:
    prefix = _windows_qgis_prefix(env)
    if prefix is None:
        return None
    install = prefix.parent.parent
    python = _first_existing(sorted(prefix.parent.glob("Python*/python.exe"), reverse=True))
    python = python or _first_existing((install / "bin/python3.exe", install / "bin/python.exe"))
    env.setdefault("QGIS_PREFIX_PATH", str(prefix))
    env.setdefault("SMART_QGIS_PLUGIN_PATH", str(prefix / "python/plugins"))
    proj = prefix.parent / "proj/share/proj"
    gdal = prefix.parent / "gdal-data"
    if proj.is_dir():
        env.setdefault("PROJ_DATA", str(proj))
    if gdal.is_dir():
        env.setdefault("GDAL_DATA", str(gdal))
    _set_path(env, install / "bin", prefix / "bin", prefix.parent / "grass/bin")
    return str(python) if python else None


def _configure_linux(env: dict[str, str]) -> str | None:
    prefix = Path(env.get("SMART_QGIS_PREFIX_PATH") or env.get("QGIS_PREFIX_PATH", "/usr"))
    plugin_candidates = [prefix / "share/qgis/python/plugins"]
    if env.get("SMART_QGIS_PLUGIN_PATH"):
        plugin_candidates.insert(0, Path(env["SMART_QGIS_PLUGIN_PATH"]))
    plugin_candidates.extend(
        (Path("/usr/share/qgis/python/plugins"), Path("/usr/local/share/qgis/python/plugins"))
    )
    plugins = _first_directory(
        plugin_candidates
    )
    env.setdefault("QGIS_PREFIX_PATH", str(prefix))
    if plugins:
        env.setdefault("SMART_QGIS_PLUGIN_PATH", str(plugins))
    return shutil.which("python3")


def worker_environment() -> tuple[str, dict[str, str]]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUNBUFFERED"] = "1"
    env["GDAL_PAM_ENABLED"] = "NO"  # Do not create sidecars alongside users' inputs.
    executable = env.get("SMART_QGIS_PYTHON")
    if sys.platform == "darwin":
        executable = executable or _configure_macos(env)
    elif sys.platform == "win32":
        executable = executable or _configure_windows(env)
    else:
        executable = executable or _configure_linux(env)
    if not executable or not Path(executable).is_file():
        raise RuntimeError(
            "Set SMART_QGIS_PYTHON to QGIS's Python executable "
            "(and SMART_QGIS_PREFIX_PATH when QGIS is not auto-discovered)"
        )
    return executable, env

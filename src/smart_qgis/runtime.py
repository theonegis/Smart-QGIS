"""Launch the distributor's Python, never mix its Qt/GDAL with the MCP venv."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def worker_environment() -> tuple[str, dict[str, str]]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONUNBUFFERED"] = "1"
    env["GDAL_PAM_ENABLED"] = "NO"  # Do not create sidecars alongside users' inputs.
    app = Path(env.get("SMART_QGIS_APP", "/Applications/QGIS.app"))
    executable = env.get("SMART_QGIS_PYTHON")
    if app.is_dir():
        contents = app / "Contents"
        executable = executable or str(contents / "MacOS/python")
        resources = contents / "Resources/qgis"
        env.setdefault("QGIS_PREFIX_PATH", str(app))
        env.setdefault("PROJ_DATA", str(resources / "proj"))
        env.setdefault("GDAL_DATA", str(resources / "gdal"))
        env["PATH"] = str(contents / "MacOS") + os.pathsep + env.get("PATH", "")
        env["SMART_QGIS_PLUGIN_PATH"] = str(resources / "python/plugins")
    else:
        executable = executable or shutil.which("python3")
        env.setdefault("SMART_QGIS_PLUGIN_PATH", "/usr/share/qgis/python/plugins")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("Set SMART_QGIS_PYTHON to a Python executable with PyQGIS installed")
    return executable, env

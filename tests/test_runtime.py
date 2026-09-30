from smart_qgis import runtime
from smart_qgis.task_store import state_root


def test_state_root_uses_platform_standard_locations(tmp_path):
    home = tmp_path / "home"
    assert state_root(platform="darwin", environ={}, home=home) == (
        home / "Library/Application Support/smart-qgis"
    )
    assert state_root(platform="linux", environ={}, home=home) == home / ".local/state/smart-qgis"
    assert state_root(platform="linux", environ={"XDG_STATE_HOME": str(tmp_path / "state")}, home=home) == (
        tmp_path / "state/smart-qgis"
    )
    assert state_root(platform="win32", environ={"LOCALAPPDATA": str(tmp_path / "local")}, home=home) == (
        tmp_path / "local/Smart-QGIS"
    )


def test_windows_runtime_discovers_qgis_distribution(tmp_path):
    prefix = tmp_path / "QGIS 4.0/apps/qgis"
    python = tmp_path / "QGIS 4.0/apps/Python312/python.exe"
    plugins = prefix / "python/plugins"
    python.parent.mkdir(parents=True)
    python.touch()
    plugins.mkdir(parents=True)
    env = {"ProgramFiles": str(tmp_path)}

    executable = runtime._configure_windows(env)

    assert executable == str(python)
    assert env["QGIS_PREFIX_PATH"] == str(prefix)
    assert env["SMART_QGIS_PLUGIN_PATH"] == str(plugins)


def test_linux_runtime_prefers_standard_qgis_plugin_path(monkeypatch, tmp_path):
    prefix = tmp_path / "prefix"
    plugins = prefix / "share/qgis/python/plugins"
    plugins.mkdir(parents=True)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: "/usr/bin/python3")
    env = {"SMART_QGIS_PREFIX_PATH": str(prefix)}

    assert runtime._configure_linux(env) == "/usr/bin/python3"
    assert env["QGIS_PREFIX_PATH"] == str(prefix)
    assert env["SMART_QGIS_PLUGIN_PATH"] == str(plugins)

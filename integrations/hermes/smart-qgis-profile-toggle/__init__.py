"""Owner-only Smart-QGIS profile switch for a single Weixin direct message."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from gateway.session_context import get_session_env


DEFAULT_HOME = Path.home() / ".hermes"
DEFAULT_CONFIG = DEFAULT_HOME / "config.yaml"
ROUTE_NAME = "owner-weixin-smart-qgis"


def _route_block(chat_id: str, user_id: str) -> str:
    return (
        f"    - name: {ROUTE_NAME}\n"
        "      platform: weixin\n"
        f'      chat_id: "{chat_id}"\n'
        f'      user_id: "{user_id}"\n'
        "      profile: smartqgis\n"
    )


def _read_config() -> str:
    return DEFAULT_CONFIG.read_text(encoding="utf-8")


def _write_config(text: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix="config.", suffix=".yaml", dir=DEFAULT_CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, DEFAULT_CONFIG)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _remove_route(text: str) -> str:
    pattern = rf"(?m)^    - name: {re.escape(ROUTE_NAME)}\n(?:^      [^\n]*\n)*"
    return re.sub(pattern, "", text)


def _set_mode(enabled: bool, chat_id: str, user_id: str) -> None:
    text = _remove_route(_read_config())
    if enabled:
        marker = "  profile_routes:\n"
        if marker not in text:
            raise RuntimeError("gateway.profile_routes is missing from the default profile")
        text = text.replace(marker, marker + _route_block(chat_id, user_id), 1)
    _write_config(text)


def _restart_default_gateway() -> None:
    time.sleep(2)
    executable = shutil.which("hermes") or "hermes"
    subprocess.Popen(
        [executable, "-p", "default", "gateway", "restart"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _handle(raw_args: str) -> str:
    action = raw_args.strip().lower()
    if action not in {"on", "off", "status", "tools"}:
        return "用法：/smart-qgis on | off | status | tools"

    chat_id = get_session_env("HERMES_SESSION_CHAT_ID").strip()
    user_id = get_session_env("HERMES_SESSION_USER_ID").strip()
    platform = get_session_env("HERMES_SESSION_PLATFORM").strip().lower()
    if platform != "weixin" or not chat_id or not user_id:
        return "此命令仅允许在已配对的微信私聊中使用。"

    enabled = ROUTE_NAME in _read_config()
    if action == "tools":
        if not enabled:
            return "当前为默认 profile。终端中运行：hermes -p default tools list --platform weixin"
        return (
            "Smart-QGIS profile 当前工具：\n"
            "- Smart-QGIS MCP：project_info、data_info、algorithm_info、task_start、"
            "task_execute、task_answer、task_update、task_resume、task_diagnose、"
            "task_restart、task_stop、prepare_algorithm\n"
            "- clarify（向用户提问）\n"
            "终端、浏览器、文件、Python、tool_search 等通用工具均未加载。"
        )
    if action == "status":
        state = "已开启（Smart-QGIS profile）" if enabled else "已关闭（默认 profile）"
        return f"Smart-QGIS 模式：{state}。"
    desired = action == "on"
    if enabled == desired:
        return "Smart-QGIS 模式已经" + ("开启。" if desired else "关闭。")
    _set_mode(desired, chat_id, user_id)
    threading.Thread(target=_restart_default_gateway, daemon=True).start()
    target = "Smart-QGIS 专用 profile" if desired else "默认 profile"
    return f"已切换到{target}；Gateway 将在约两秒后重启，请在重启完成后发送下一条消息。"


def register(ctx):
    ctx.register_command(
        "smart-qgis",
        _handle,
        description="切换 Smart-QGIS 专用微信 profile",
        args_hint="on|off|status",
    )

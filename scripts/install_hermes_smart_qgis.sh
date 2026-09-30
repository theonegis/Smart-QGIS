#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "$0")/.." && pwd)
hermes_home=${HERMES_HOME:-"$HOME/.hermes"}
plugin_source="$repo_root/integrations/hermes/smart-qgis-profile-toggle"
profile_home="$hermes_home/profiles/smartqgis"

command -v hermes >/dev/null || {
  echo "Hermes CLI was not found on PATH." >&2
  exit 1
}

if [[ ! -d "$profile_home" ]]; then
  hermes profile create smartqgis --clone-from default --no-alias \
    --description "Smart-QGIS 专用微信工作环境：仅暴露 Smart-QGIS MCP 与澄清工具。"
fi

for target_home in "$hermes_home" "$profile_home"; do
  target="$target_home/plugins/smart-qgis-profile-toggle"
  mkdir -p "$target"
  cp "$plugin_source/plugin.yaml" "$plugin_source/__init__.py" "$target/"
done

python3 - "$hermes_home/config.yaml" "$profile_home/config.yaml" <<'PY'
from pathlib import Path
import sys

plugin = "smart-qgis-profile-toggle"

def enable_plugin(text: str) -> str:
    if f"    - {plugin}\n" in text:
        return text
    marker = "  enabled:\n"
    plugins_start = text.find("plugins:\n")
    if plugins_start < 0:
        raise RuntimeError("plugins section is missing")
    marker_at = text.find(marker, plugins_start)
    if marker_at < 0:
        raise RuntimeError("plugins.enabled is missing")
    line_end = text.find("\n", marker_at) + 1
    return text[:line_end] + f"    - {plugin}\n" + text[line_end:]

def configure_weixin(text: str) -> str:
    block = "  weixin:\n    - mcp-smart-qgis\n    - clarify\n"
    if block in text:
        return text
    if "  weixin:\n" in text:
        raise RuntimeError("smartqgis profile already defines platform_toolsets.weixin; configure it manually")
    marker = "platform_toolsets:\n"
    if marker not in text:
        raise RuntimeError("platform_toolsets section is missing")
    return text.replace(marker, marker + block, 1)

def configure_smart_qgis_skill(text: str) -> str:
    skill = "smart-qgis-mcp"
    marker = "skills:\n"
    if marker not in text:
        return text + f"\nskills:\n  auto_load:\n    - {skill}\n"

    start = text.index(marker) + len(marker)
    end = len(text)
    for line_start in range(start, len(text)):
        if line_start > start and text[line_start - 1] != "\n":
            continue
        line_end = text.find("\n", line_start)
        if line_end < 0:
            line_end = len(text)
        line = text[line_start:line_end]
        if line and not line[0].isspace():
            end = line_start
            break
    block = text[start:end]
    if f"    - {skill}\n" in block:
        return text
    if "  auto_load:" in block:
        raise RuntimeError(
            "skills.auto_load already exists without smart-qgis-mcp; configure it manually"
        )
    return text[:start] + f"  auto_load:\n    - {skill}\n" + text[start:]

default, smart = map(Path, sys.argv[1:])
default.write_text(enable_plugin(default.read_text(encoding="utf-8")), encoding="utf-8")
smart_text = enable_plugin(smart.read_text(encoding="utf-8"))
smart_text = configure_weixin(smart_text)
smart.write_text(configure_smart_qgis_skill(smart_text), encoding="utf-8")
PY

echo "Installed Smart-QGIS profile toggle. Restart Hermes Gateway once:"
echo "  hermes -p default gateway restart"
echo "Then use /smart-qgis on, /smart-qgis off, or /smart-qgis status in the paired Weixin DM."

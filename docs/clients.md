# Agent 客户端接入

Smart-QGIS 只提供后台 GIS 工具。客户端负责选择云端/本地模型、规划步骤、保留对话和审批工具调用。

## Codex

在 Codex 的配置文件中添加（替换绝对路径）：

```toml
[mcp_servers.smart_qgis]
command = "/path/to/Smart-QGIS/.venv/bin/python"
args = ["-m", "smart_qgis.server"]
startup_timeout_sec = 90
tool_timeout_sec = 900
```

也可使用 `codex mcp add smart_qgis -- /path/to/Smart-QGIS/.venv/bin/python -m smart_qgis.server`。重启客户端后检查 MCP 工具列表。交互使用保留客户端默认的工具审批策略。

`codex exec` 非交互验收不能弹出审批；本仓库 `scripts/client_case.py` 为一次明确授权的测试进程设置 `smart_qgis` 的 `default_tools_approval_mode="approve"`，不修改全局配置。脚本只应在确认输入和输出路径后执行。

## Hermes + Ollama

在 Hermes 的 `config.yaml` 合并以下片段：

```yaml
model:
  default: your-local-tool-calling-model
  provider: custom
  base_url: http://127.0.0.1:11434/v1
  context_length: 65536
mcp_servers:
  smart_qgis:
    command: /path/to/Smart-QGIS/.venv/bin/python
    args: ["-m", "smart_qgis.server"]
    connect_timeout: 90
    timeout: 900
    supports_parallel_tool_calls: false
```

使用 `ollama list` 选择已安装、支持工具调用的模型；确保模型实际上下文长度与 Hermes 配置一致。Hermes 的 CLI 工具集筛选参数使用服务器名称：`--toolsets smart_qgis`。

不需要把模型放进 Smart-QGIS 的 Python 环境。客户端与 MCP 的 stdio 会话在整个工作流中保持连接；每次启动一个新 MCP 进程都会获得新工程。

## 通用注意事项

- 可通过 MCP 工具 `project(action="info")` 验证 QGIS 版本、算法提供者及状态。
- 先查询 `algorithms(action="help", algorithm="...")`，再执行 `run_processing`。
- 工具调用失败会返回 MCP `isError`；客户端应根据错误修正参数，不应把失败当成功。
- 同一会话工具调用串行执行。多个客户端分别启动 MCP 时相互隔离，不能同时写同一个输出文件。
- 本服务是可信本地工具，具有进程用户的文件和网络权限；它不是多租户沙箱。不要将 stdio 包装为未鉴权的公网服务。

参考：[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Hermes MCP 配置](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md)。

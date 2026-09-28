# Agent 客户端接入

Smart-QGIS 只提供后台 GIS 工具。客户端负责选择云端/本地模型、规划步骤、保留对话和审批工具调用。

当前工作树默认启用新的 `reliable` 任务接口：Agent 创建任务并自动生成最小契约，再执行修改工具。见[可靠任务使用说明](reliable-usage.md)。`--execution-mode legacy` 仅用于论文中的旧直接工具消融基线，不是兼容承诺。`scripts/paper_case.py` 是旧基线示例；`scripts/client_case.py` 默认运行新接口，实验时可显式选择旧基线。两个模式的结果不能混用。

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
  ollama_num_ctx: 65536  # Request the same runtime window; verify server support
mcp_servers:
  smart_qgis:
    command: /path/to/Smart-QGIS/.venv/bin/python
    args: ["-m", "smart_qgis.server"]
    connect_timeout: 90
    timeout: 900
    supports_parallel_tool_calls: false
```

使用 `ollama list` 选择已安装、支持工具调用的模型；确保模型实际上下文长度与 Hermes 配置一致。Hermes 的 CLI 工具集筛选参数使用服务器名称：`--toolsets smart_qgis`。

不需要把模型放进 Smart-QGIS 的 Python 环境。客户端与 MCP 的 stdio 会话在整个工作流中保持连接；新 MCP 进程从空工程启动，可靠模式用 `task_recover` 附着已有任务并恢复检查点。必须沿用相同的 `SMART_QGIS_STATE_DIR`。

本地模型测试可指定 `--context-length` 和可选 `--reasoning-effort`。标准制图只需 `task_start → task_execute_next`；第二次调用只复制一个 opaque token。通用处理使用精确算法 ID 调用 `prepare_algorithm`，按需以 `task_answer` 回答其结构化问题；成功路径不要插入 `task_diagnose`。详见[性能诊断及对照计划](local-model-performance.md)。

## 通用注意事项

- 可靠模式通过任务诊断和只读发现接口检查环境；`project_manage` 等直接 GIS 工具只在论文旧接口基线中公开。
- 标准地图/工程使用 `task_start → task_execute_next`；通用 Processing 使用 `prepare_algorithm → task_answer（仅缺参时）→ task_execute_next`。
- 工具调用失败会返回 MCP `isError`；客户端应根据错误修正参数，不应把失败当成功。
- 同一会话工具调用串行执行。多个客户端分别启动 MCP 时相互隔离，不能同时写同一个输出文件。
- 本服务是可信本地工具，具有进程用户的文件和网络权限；它不是多租户沙箱。不要将 stdio 包装为未鉴权的公网服务。

参考：[Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)、[Hermes MCP 配置](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md)。

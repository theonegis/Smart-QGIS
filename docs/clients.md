# MCP 客户端接入

Smart-QGIS 只提供后台 GIS 工具；模型、对话历史、思考超时和审批由客户端负责。默认使用 `reliable`；只有明确需要无任务日志的直接工具接口时，才在服务器参数里加入 `--execution-mode legacy`。请用绝对路径并确认输出目录。

## Codex

在 Codex 配置中加入（替换仓库绝对路径）：

```toml
[mcp_servers.smart_qgis]
command = "/path/to/Smart-QGIS/.venv/bin/python"
args = ["-m", "smart_qgis.server"]
startup_timeout_sec = 90
tool_timeout_sec = 900
```

检查客户端实际列出的 Smart-QGIS 工具。非交互测试的自动审批只应在一次明确授权、隔离的实验配置中设置，不要改成全局无条件信任。

## Pi + Ollama

Pi 需要其 MCP 适配扩展。可在项目/试次的独立 MCP 配置文件中注册服务器，再用 `pi --mcp-config /absolute/path/to/config.json` 启动；若在测试中自动批准工具调用，应只对这个明确配置的项目服务器授权，不修改全局信任策略。每个独立实验样本使用新的 Pi session；同一任务发生错误时继续原 session 和原 Smart-QGIS task ID。模型上下文、推理强度和思考计时由 Pi/Ollama 一侧设置，不能通过 MCP 的 `--timeout` 代替。

配置文件中服务器条目的核心形式如下；适配扩展及审批字段按已安装 Pi 版本检查：

```json
{
  "mcpServers": {
    "smart-qgis": {
      "command": "/path/to/Smart-QGIS/.venv/bin/python",
      "args": ["-m", "smart_qgis.server"],
      "directTools": true
    }
  }
}
```

## 共同约定

可靠模式的标准地图/工程为 `task_start → task_execute()`；通用 Processing 为 `task_start → 确认算法 ID → prepare_algorithm → task_execute()`，仅在返回真实缺参问题时才询问用户并调用 `task_answer`。工程、数据、样式、底图和制图修订分别使用 `project_info`、`data_info` 与 `task_update`；所有调用以当前会话的 MCP 工具列表和参数 schema 为准，不能猜不存在的工具。失败后诊断、用户指导及恢复见[用法说明](reliable-usage.md)。

一个 MCP 进程的 QGIS 操作串行执行。不同客户端不应同时写同一任务状态目录或输出文件。新进程须用相同 `SMART_QGIS_STATE_DIR` 和原 task ID 恢复。该本地工具具有运行用户的文件权限，不应直接暴露为无鉴权公网服务。

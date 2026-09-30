# 开发与架构概览

Smart-QGIS 2.0 是 stdio MCP 服务，不含模型或聊天界面。MCP 服务与 QGIS Python 分离；PyQGIS 在无 GUI 的独立 worker 主线程内运行，bridge 串行转发并管理超时。工具参数由 Pydantic/JSON Schema 校验；Processing 算法参数以当前 QGIS 注册表帮助为准，而非固定的少量算法清单。

```text
Codex / Pi / 其他 MCP 客户端
             │ stdio
server.py + tools.py / task_tools.py
             │
coordinator.py + SQLite task_store.py（默认 reliable）
             │
bridge.py → worker.py / extra_ops.py → QGIS Processing、工程、布局
```

## 两种公开接口

默认 `reliable` 公开精简任务工具与 `algorithm_info`。服务端检查输入、建立最小任务、按步骤准备算法、执行并核验基础输出，将步骤/尝试/问题/检查点保存在 SQLite。标准工作流用无参数 `task_execute`；通用处理用 `prepare_algorithm`。失败恢复及真实用户干预见[契约与恢复](task-contracts-and-recovery.md)和[调用说明](reliable-usage.md)。

`--execution-mode legacy` 公开无任务日志的直接 GIS 工具；不承诺恢复、兼容或与默认模式相同的工具名。两种模式都通过同一个独立 QGIS worker 执行地理操作，区别在任务控制与公开工具表。客户端不能推断未列出的工具。

## 扩展原则

新增 GIS 操作时，在 `tools.py` 定义清楚的参数模型与工具描述，并在 worker 中实现实际操作；新增可靠任务能力时，同时维护任务状态、错误语义和回归测试。处理算法通常不需要为每个 ID 添加专用 MCP 函数：先由当前 QGIS 注册表发现精确 ID，再让统一准备接口读取它的帮助并校验参数。

服务端始终使用完整 Pydantic 模型验证调用；对客户端发布 schema 时只移除自动生成的字段标题和重复类级说明，保留字段说明、必填关系、枚举、范围及禁止额外字段等验证规则。项目 Skill 只保留会改变调用决策的规则，避免把重复教程长期放入本地模型上下文。

保持契约简单：只注入基础输入/参数/输出检查及用户明确要求，不把单一任务的科学答案、样式偏好或反复纠错经验强行写成所有任务的执行门槛。必要但不明确的 CRS、字段、枚举、波段或方法应成为用户问题。制图的一般指导放在[项目 Skill](../skills/smart-qgis-mcp/SKILL.md)，不等于自动化已证明地图美观。标题、图例标题、元素位置和页面方向属于可持久化的展示参数；用户修订后服务端仅失效布局及其导出，保留已核验的数据分析成果，并以数据外接范围自适应页面和主图框。

## 运行环境与验证

运行时自动发现 macOS 的 `/Applications/QGIS.app`、Windows `Program Files` 下的 `QGIS*/apps/qgis`，以及 Ubuntu/Fedora 的系统 QGIS 路径。若 QGIS 位于非标准位置，优先设置 `SMART_QGIS_PYTHON` 为该发行版自带且能导入 PyQGIS 的 Python；必要时同时设置 `SMART_QGIS_PREFIX_PATH`（或 `QGIS_PREFIX_PATH`）。`SMART_QGIS_PLUGIN_PATH`、`PROJ_DATA` 和 `GDAL_DATA` 只在发行版未自行配置时使用。安装的 QGIS provider 因机器而异；实际调用前查询算法。

任务 SQLite 日志位于各系统标准的持久状态目录：macOS 为 `~/Library/Application Support/smart-qgis`，Windows 为 `%LOCALAPPDATA%/Smart-QGIS`，Ubuntu/Fedora 为 `$XDG_STATE_HOME/smart-qgis`（默认 `~/.local/state/smart-qgis`）。未由用户指定的成果文件位于系统临时目录的 `smart-qgis/<task-id>`；指定绝对 `path` 或 `directory` 时，成果直接写入该位置。

```bash
uv sync --locked
uv run ruff check src tests
uv run pytest -q
```

本地原始数据、Agent 轨迹、地图以及研究评价材料不上传 GitHub；公开测试应使用合成数据或可再分发输入。

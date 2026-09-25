# 2.0 开发说明

## 架构

```text
Codex / Hermes / 其他 MCP 客户端
                │ stdio MCP
          server.py（官方 MCP SDK）
                │ 校验参数并调用
          tools.py（LangChain StructuredTool + Pydantic）
                │ 异步串行调用
          bridge.py（子进程生命周期、超时、错误）
                │ 私有 JSON-lines 管道
          worker.py / extra_ops.py（QGIS 自带 Python）
                │ 主线程、QT_QPA_PLATFORM=offscreen
       QgsApplication + QgsProject + Processing + Layout
```

所有 QGIS 对象只在 worker 主线程操作。MCP 与 QGIS 使用不同的 Python 环境，不把 Qt/GDAL 二进制依赖混进 uv 环境。Qt 作为 QGIS 的底层依赖仍然存在，但没有桌面窗口、聊天面板、`iface`、QGIS 插件注册或 TCP Socket 转发。

LangChain 用于工具定义、参数验证和执行接口，不创建模型、不发起推理、不读取 Agent 凭据。客户端 harness 是唯一的推理编排层。

## 模块与工具

| 模块/工具 | 职责 |
|---|---|
| `runtime.py` | 定位 QGIS Python、PROJ/GDAL 和 Processing 插件路径 |
| `server.py` | MCP 工具列表、调用、资源、提示模板 |
| `bridge.py` | 每服务一个 worker；串行请求；取消/超时终止进程组 |
| `project` | info/create/open/save；保存工程和布局 |
| `load_data`, `add_basemap` | 本地/提供者数据源；XYZ/WMS/WMTS |
| `layers`, `features` | 显隐、排序、名称、删除、属性和几何抽样 |
| `style_vector`, `style_graduated` | 单一、分类、分级符号；标签 |
| `style_raster`, `render_raster` | 伪彩色、灰度、RGB、山体阴影 |
| `style_file` | 加载/保存 QML |
| `vector_data` | 选择、统计、内存数据、矢量导出与 CRS 转换 |
| `algorithms`, `run_processing` | 查询完整算法注册表与参数，执行处理并加载结果 |
| `layout`, `export_map` | 布局、QPT、地图图片/PDF |

使用 `algorithms` 的分页、关键字搜索来发现算法；提供者由安装的 QGIS 决定，不能假设每台机器都包含 GRASS/PDAL。不是只支持示例中列出的几个算法。未识别的算法 ID/参数会被拒绝。没有开放任意 Python `eval/exec`；1.0 中的任意代码执行由通用 Processing 和明确工具接口替代。

## 运行环境

macOS 自动发现 `/Applications/QGIS.app/Contents/MacOS/python`。自定义安装可使用：

- `SMART_QGIS_APP`：macOS QGIS.app 路径。
- `SMART_QGIS_PYTHON`：能够 `import qgis.core` 的 Python 可执行文件。
- `QGIS_PREFIX_PATH`：QGIS 安装前缀。
- `SMART_QGIS_PLUGIN_PATH`：包含 `processing` 的 QGIS Python plugins 目录。
- `PROJ_DATA` / `GDAL_DATA`：必要时指定 QGIS 自带资源目录。

Linux 例如使用 `/usr/bin/python3`、前缀 `/usr`、plugins 路径 `/usr/share/qgis/python/plugins`。先通过系统包管理器安装 QGIS Python 绑定。macOS 4.2.2 是本次实测环境；Linux 与 QGIS 3.44 需在各自机器验证，不宣称已跨平台测试。当前进程组管理面向 POSIX，Windows 尚未验收。

MCP SDK 明确约束 `>=1.28,<2`，采用官方 v1 维护线的 stdio 接口，`uv.lock` 固定实际依赖版本，避免客户端生态升级时隐式迁移到 SDK v2。Smart-QGIS **产品版本 2.0** 与 MCP SDK 主版本无关。

## 数据与状态语义

- `project.create/open` 替换当前状态；先保存需要保留的工程。
- 工程保存不复制输入文件；相对路径由 QGIS 管理，迁移工程时须一并迁移数据。
- 内存图层不具有跨进程持久性，保存前要求导出为文件并重新加载。
- `TEMPORARY_OUTPUT` 仅在本服务会话存活期间有效，需永久保存时指定明确输出路径。
- 图层引用优先用 ID；重复名称会报错。排序和布局图层列表从上到下。
- 布局采用指定 CRS，对数据范围做坐标转换；自动范围排除全球底图，可用 `extent_layer` 或显式 extent。
- DEM 色带使用有效像素统计，NoData 保持透明。坡度/山体阴影需要处理水平与垂直单位；示例先投影到米制 CRS 再算坡度。
- 操作串行但不是事务。处理工具可能留下部分文件；超时/取消会终止 worker，之后要求重启 MCP 并重开已保存工程，避免悄悄丢失状态后继续执行。
- 通过 stdout 的原生库输出重定向到 stderr，防止污染 MCP 和内部管道。
- 工作进程使用临时配置；GDAL PAM 禁用，避免向输入目录写 `.aux.xml`。

## 扩展与验证

在 `tools.py` 添加 Pydantic 参数模型与 `SPECS`，在 worker 中添加对应操作。所有 MCP 入口经同一 LangChain 参数模型校验。返回 JSON 可序列化结果，失败抛出异常，由 MCP SDK 转换为错误响应。

```bash
uv sync --locked
uv run ruff check src scripts tests
uv run pytest -q
```

单元测试验证参数契约与独立工具分发；集成测试实际启动 QGIS/MCP，验证矢量选择/导出、分类样式、缓冲区、栅格渲染、坡度、布局持久化和导出。案例脚本及真实客户端验收见 [testing.md](testing.md)。

## 参考资料

- [PyQGIS 3.44 独立脚本](https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/intro.html)
- [QGIS API](https://api.qgis.org/api/)
- [MCP Python SDK](https://py.sdk.modelcontextprotocol.io/) 与 [v1 文档](https://py.sdk.modelcontextprotocol.io/v1/)
- [LangChain 工具](https://docs.langchain.com/oss/python/langchain/tools)

本地论文用于确定“加载—裁剪—可视化—制图”验收过程，不随代码分发。案例数据、地图、原始日志和临时 Agent 配置保留在被忽略的 `artifacts/`。
